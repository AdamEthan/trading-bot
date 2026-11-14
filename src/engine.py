"""The trading loop: poll prices, ask the strategy, check risk, paper-fill, log.

One run covers one session (by default from now until session.end). At the
end the bot closes any open position, writes summary.json, and stops.
"""
from __future__ import annotations

import platform
import traceback
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .brokers.paper import PaperBroker
from .config import ROOT
from .data.feed import closed_bars, make_feed
from .data.models import Bar, Quote, utc_iso
from .data.news import NewsCalendar
from .execution.orders import OrderManager, side_of
from .risk.limits import RiskManager
from .strategy.interface import PositionView, Signal, Target
from .strategy.registry import build_strategy, file_fingerprint
from .utils.logging import Journal, console_logger

HISTORY_LIMIT = 2000
KILL_FILE = "KILL"


def session_window(cfg: dict, now: float) -> tuple[float, float]:
    """Unix start and end of today's session in the configured time zone."""
    tz = None
    if cfg["timezone"]:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg["timezone"])
    local = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now).astimezone()

    def at(hhmm: str) -> datetime:
        hour, minute = map(int, hhmm.split(":"))
        return local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    start = local if cfg["start"] == "now" else at(cfg["start"])
    end = at(cfg["end"])
    if cfg["start"] != "now" and end <= start:
        end += timedelta(days=1)  # overnight session, e.g. 22:00 -> 06:00
    return start.timestamp(), end.timestamp()


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@dataclass
class ExitLatch:
    """After a stop or target exit, wait for the strategy to change its mind before re-entering."""
    side: Target | None = None


class Engine:
    def __init__(self, cfg: dict, run_dir: Path, *, feed=None, news: NewsCalendar | None = None,
                 clock=time.time, sleep=time.sleep, minutes: float | None = None, config_path: Path | None = None):
        self.cfg = cfg
        self.clock, self.sleep = clock, sleep
        self.run_dir = Path(run_dir)
        self.run_id = self.run_dir.name
        self.config_path = config_path
        self.journal = Journal(self.run_dir, clock)
        self.log = console_logger(self.run_dir)

        m, a = cfg["market"], cfg["account"]
        self.symbol, self.bar_seconds, self.poll_seconds = m["symbol"], m["bar_seconds"], m["poll_seconds"]
        self.feed = feed or make_feed(m["source"], m["symbol"])
        self.news = news or NewsCalendar(cfg["news"], ROOT / "data", clock=clock)
        self.strategy = build_strategy(cfg["strategy"], ROOT)
        self.broker = PaperBroker(a["starting_cash"], a["fee_bps"], a["slippage_bps"])
        self.risk = RiskManager(cfg["risk"])
        self.orders = OrderManager(self.run_id, self.broker, self.risk, a["position_size_usd"])

        now = clock()
        self.start_ts, self.end_ts = session_window(cfg["session"], now)
        if minutes is not None:
            self.start_ts, self.end_ts = now, now + minutes * 60
        self.history: list[Bar] = []
        self.quote: Quote | None = None
        self.last_signal: dict | None = None
        self.entry_bar: int | None = None
        self.latch = ExitLatch()
        self.state = "starting"
        self.errors_in_row = 0
        self.strategy_errors = 0
        self.last_error = ""
        self.start_equity = a["starting_cash"]
        self.recent: list[dict] = []

    # ---------- helpers ----------
    def say(self, kind: str, message: str, **data) -> None:
        self.log.info(message)
        record = self.journal.event(kind, message=message, **data)
        self.recent = (self.recent + [record])[-50:]

    @property
    def trades_today(self) -> int:
        """Entries so far. A trade is one entry plus its exit; exits are never limited."""
        return sum(1 for f in self.broker.fills if f.reason.startswith("entry"))

    def mark(self) -> float | None:
        if self.quote:
            return self.quote.price
        return self.history[-1].close if self.history else None

    def day_pnl(self) -> float:
        mark = self.mark()
        return (self.broker.equity(mark) if mark else self.broker.cash) - self.start_equity

    def position_view(self) -> PositionView:
        qty = self.broker.position
        held = 0
        if self.entry_bar is not None and self.history:
            held = max(0, (self.history[-1].start - self.entry_bar) // self.bar_seconds)
        return PositionView(side_of(qty), qty, self.broker.avg_price or None, held)

    def kill_requested(self) -> bool:
        return (self.run_dir / KILL_FILE).exists()

    # ---------- data ----------
    def fetch_quote(self) -> Quote | None:
        try:
            self.quote = self.feed.quote()
            self.errors_in_row = 0
            return self.quote
        except Exception as exc:  # any feed glitch: log it, keep the run alive
            self.data_error(exc)
            return None

    def data_error(self, exc: Exception) -> None:
        self.errors_in_row += 1
        self.last_error = str(exc)
        if self.errors_in_row in (1, 5) or self.errors_in_row % 30 == 0:
            self.say("data_error", f"Data problem ({self.errors_in_row} in a row): {exc}")

    def new_bars(self) -> list[Bar]:
        try:
            bars = closed_bars(self.feed.candles(self.bar_seconds), self.clock())
        except Exception as exc:
            self.data_error(exc)
            return []
        last = self.history[-1].start if self.history else -1
        fresh = [b for b in bars if b.start > last]
        self.history = (self.history + fresh)[-HISTORY_LIMIT:]
        return fresh

    # ---------- trading ----------
    def execute(self, target: Target, reason: str, *, forced: bool = False) -> None:
        if self.quote is None:
            return
        news = None
        if not forced:
            event = self.news.blackout(self.clock())
            news = f"{event.country} {event.title}" if event else None
        fills, rejected = self.orders.move_to(
            target, self.quote, reason, now=self.clock(), trades_today=self.trades_today,
            day_pnl=self.day_pnl(), news=news)
        for fill in fills:
            self.journal.fill(fill)
            self.entry_bar = self.history[-1].start if self.history and fill.position_after else None
            pnl = f", realized ${fill.realized_pnl:+,.2f}" if fill.realized_pnl else ""
            self.say("fill", f"PAPER {fill.side.upper()} {fill.qty:.6f} {self.symbol} @ ${fill.price:,.2f} "
                             f"(market ${fill.market_price:,.2f}, fee ${fill.fee:,.2f}{pnl}) - {fill.reason}",
                     fill=fill.row())
        for message in rejected:
            self.say("order_refused", message)

    def flatten(self, reason: str) -> None:
        if side_of(self.broker.position) != Target.FLAT:
            if self.quote is None or self.clock() - self.quote.time > 30:
                self.fetch_quote()
            self.execute(Target.FLAT, reason, forced=True)

    def on_signal(self, signal: Signal) -> None:
        target = signal.target
        if target == Target.SHORT and not self.risk.allow_short:
            target = Target.FLAT
        if self.latch.side is not None:
            if target == self.latch.side:
                target = Target.FLAT  # stopped out: stay out until the strategy flips
            else:
                self.latch.side = None
        bar = self.history[-1]
        self.last_signal = {"bar": utc_iso(bar.start), "target": signal.target.value,
                            "acted_on": target.value, "reason": signal.reason}
        self.journal.event("signal", **self.last_signal, close=bar.close)
        if target != side_of(self.broker.position):
            self.execute(target, signal.reason)

    def check_exits(self) -> None:
        qty, price = self.broker.position, self.mark()
        if not qty or not price or not self.broker.avg_price:
            return
        move = (price - self.broker.avg_price) / self.broker.avg_price * 100 * (1 if qty > 0 else -1)
        stop, take = self.cfg["exits"]["stop_loss_pct"], self.cfg["exits"]["take_profit_pct"]
        if stop and move <= -stop:
            self.latch.side = side_of(qty)
            self.say("stop_loss", f"Stop loss: {move:.2f}% against the position")
            self.execute(Target.FLAT, f"stop loss {move:.2f}%", forced=True)
        elif take and move >= take:
            self.latch.side = side_of(qty)
            self.say("take_profit", f"Take profit: {move:.2f}% in favour")
            self.execute(Target.FLAT, f"take profit {move:.2f}%", forced=True)

    def check_risk(self) -> None:
        if self.risk.halted:
            return
        pnl = self.day_pnl()
        if self.risk.kill_switch and self.risk.loss_limit_hit(pnl):
            self.risk.halt(f"daily loss ${-pnl:,.2f} hit the ${self.risk.max_daily_loss_usd:,.2f} limit")
            self.say("halt", f"KILL SWITCH: {self.risk.halt_reason}. Closing position, no new trades today.")
            self.flatten("daily loss limit")
        if self.kill_requested():
            self.risk.halt("kill switch pressed")
            self.say("halt", "KILL SWITCH pressed. Closing position, no new trades today.")
            self.flatten("kill switch")

    def check_news(self) -> None:
        if not self.cfg["news"]["flatten_before"]:
            return
        event = self.news.blackout(self.clock())
        if event and side_of(self.broker.position) != Target.FLAT:
            self.say("news_flatten", f"Closing before news: {event.country} {event.title}")
            self.flatten(f"news: {event.title}")

    # ---------- snapshots ----------
    def status(self) -> dict:
        mark = self.mark()
        qty = self.broker.position
        now = self.clock()
        blackout = self.news.blackout(now)
        return {
            "state": self.state, "run_id": self.run_id, "updated": utc_iso(now),
            "symbol": self.symbol, "source": self.feed.name, "bar_seconds": self.bar_seconds,
            "session": {"start": utc_iso(self.start_ts), "end": utc_iso(self.end_ts)},
            "price": mark, "quote_time": utc_iso(self.quote.time) if self.quote else None,
            "position": {"qty": qty, "side": side_of(qty).value, "avg_price": self.broker.avg_price or None,
                         "unrealized": self.broker.unrealized(mark) if mark else 0.0},
            "account": {"starting_cash": self.start_equity, "cash": self.broker.cash,
                        "equity": self.broker.equity(mark) if mark else self.broker.cash,
                        "day_pnl": self.day_pnl(), "realized": self.broker.realized, "fees": self.broker.fees},
            "risk": {"trades_today": self.trades_today, "max_trades": self.risk.max_trades,
                     "max_daily_loss": self.risk.max_daily_loss_usd, "max_position_usd": self.risk.max_position_usd,
                     "halted": self.risk.halted, "halt_reason": self.risk.halt_reason},
            "strategy": {"name": self.strategy.name, "params": self.strategy.p,
                         "warmup": self.strategy.warmup_bars(), "bars": len(self.history)},
            "last_signal": self.last_signal,
            "news": {"enabled": self.news.enabled, "error": self.news.error,
                     "blackout": blackout.to_dict() if blackout else None,
                     "upcoming": [e.to_dict() for e in self.news.upcoming(now)][:8]},
            "data": {"errors_in_row": self.errors_in_row, "last_error": self.last_error},
            "recent": self.recent[-20:],
        }

    def write_status(self) -> None:
        try:
            self.journal.write("status.json", self.status())
        except OSError as exc:  # the dashboard snapshot is not worth stopping for
            self.log.warning(f"Could not update status.json: {exc}")

    def record_equity(self) -> None:
        mark = self.mark()
        if mark:
            self.journal.equity(now=self.clock(), price=mark, cash=self.broker.cash, position=self.broker.position,
                                equity=self.broker.equity(mark), day_pnl=self.day_pnl())

    def summary(self) -> dict:
        s = self.status()
        sells = [f for f in self.broker.fills if f.realized_pnl]
        return {
            "run_id": self.run_id, "symbol": self.symbol, "source": self.feed.name,
            "strategy": s["strategy"]["name"], "started": utc_iso(self.start_ts), "ended": utc_iso(self.clock()),
            "starting_cash": self.start_equity, "final_cash": self.broker.cash,
            "final_position": self.broker.position, "final_price": self.mark(),
            "final_equity": s["account"]["equity"], "day_pnl": s["account"]["day_pnl"],
            "realized_pnl": self.broker.realized, "fees": self.broker.fees, "fills": len(self.broker.fills),
            "closed_trades": len(sells), "winning_trades": sum(1 for f in sells if f.realized_pnl > 0),
            "halted": self.risk.halted, "halt_reason": self.risk.halt_reason,
        }

    # ---------- main ----------
    def start(self) -> None:
        cfg = self.cfg
        self.journal.write("run.json", {
            "run_id": self.run_id, "created": utc_iso(self.clock()), "mode": "paper",
            "code_commit": git_commit(), "python": platform.python_version(),
            "strategy_class": type(self.strategy).__name__, "strategy_params": self.strategy.p,
            "strategy_file_sha256": file_fingerprint(cfg["strategy"], ROOT),
            "session_start": utc_iso(self.start_ts), "session_end": utc_iso(self.end_ts),
            "config": cfg,
        })
        self.say("start", f"Run {self.run_id}: paper trading {self.symbol} from {self.feed.name}, "
                          f"strategy {self.strategy.name} {self.strategy.p}")
        self.say("session", f"Session {datetime.fromtimestamp(self.start_ts).strftime('%H:%M')} to "
                            f"{datetime.fromtimestamp(self.end_ts).strftime('%Y-%m-%d %H:%M')} (this computer's time)")
        self.news.refresh()
        if self.news.enabled:
            todays = self.news.upcoming(self.clock())
            self.say("news", f"News filter on: {len(todays)} {'/'.join(cfg['news']['impact'])}-impact "
                             f"{'/'.join(cfg['news']['currencies'])} event(s) in the next 24h"
                             + (f" (calendar error: {self.news.error})" if self.news.error else ""))

    def warm_up(self) -> None:
        self.new_bars()
        for bar in self.history:
            self.journal.bar(bar)
        need = self.strategy.warmup_bars()
        self.say("warmup", f"Loaded {len(self.history)} closed {self.bar_seconds}s bars "
                           f"(strategy needs {need})")

    def run(self) -> dict:
        self.start()
        try:
            self.state = "waiting"
            while self.clock() < self.start_ts and not self.kill_requested():
                self.write_status()
                self.sleep(min(30, max(1, self.start_ts - self.clock())))
            self.warm_up()
            self.state = "running"
            self.fetch_quote()
            self.record_equity()
            self.evaluate()
            while self.clock() < self.end_ts:
                self.tick()
                if self.risk.halted and side_of(self.broker.position) == Target.FLAT:
                    self.say("stopped", "Trading halted for the day; ending the run.")
                    break
                self.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            self.say("interrupt", "Stopped by Ctrl+C")
        except Exception as exc:  # log the crash, then still close out the paper position
            self.state = "error"
            self.say("crash", f"Bot crashed: {exc!r}")
            raise
        finally:
            self.finish()
        return self.summary()

    def tick(self) -> None:
        self.fetch_quote()
        self.news.refresh()
        self.check_risk()
        self.check_exits()
        self.check_news()
        fresh = self.new_bars()
        for bar in fresh:
            self.journal.bar(bar)
        if fresh:
            self.evaluate()
            self.record_equity()
        self.state = "halted" if self.risk.halted else "running"
        self.write_status()

    def evaluate(self) -> None:
        """Ask the strategy about the newest closed bar and act on its answer."""
        if self.risk.halted or not self.history or len(self.history) < self.strategy.warmup_bars():
            return
        if self.clock() - self.history[-1].end > 2 * self.bar_seconds + 30:
            self.say("stale", "Newest bar is old (computer asleep or feed lagging); skipping this signal")
            return
        try:
            signal = self.strategy.on_bar(self.history, self.position_view())
            if not isinstance(signal, Signal):
                raise TypeError(f"on_bar returned {signal!r}, expected a Signal")
        except Exception as exc:  # a bug in strategy code skips this bar instead of ending the day
            self.strategy_errors += 1
            self.say("strategy_error", f"Strategy error ({self.strategy_errors} in a row), bar skipped: {exc!r}",
                     traceback=traceback.format_exc())
            if self.strategy_errors >= 5:
                self.risk.halt("strategy failed 5 bars in a row")
                self.say("halt", f"Halting: {self.risk.halt_reason}. Closing position.")
                self.flatten("strategy errors")
            return
        self.strategy_errors = 0
        self.on_signal(signal)

    def finish(self) -> None:
        if self.cfg["session"]["flatten_at_end"] or self.risk.halted:
            self.flatten("end of session")
        self.record_equity()
        if self.state != "error":
            self.state = "finished"
        summary = self.summary()
        self.journal.write("summary.json", summary)
        self.write_status()
        self.say("summary", (
            f"Done. Day P&L ${summary['day_pnl']:+,.2f} | equity ${summary['final_equity']:,.2f} | "
            f"{summary['fills']} fills, {summary['closed_trades']} closed trades "
            f"({summary['winning_trades']} winners) | fees ${summary['fees']:,.2f}"))
        self.say("files", f"Logs: {self.run_dir}  -  check them with: python run.py verify {self.run_dir}")
