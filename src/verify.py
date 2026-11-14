"""Re-check a finished (or running) run's logs.

Offline checks need nothing but the log folder:
  1. Replaying trades.csv from the starting cash reproduces every cash and
     position column, and the totals in summary.json.
  2. Every fee equals fill price x quantity x fee rate.
  3. Every fill price equals the market price worsened by the slippage rate.
  4. No entry broke the position-size limit, and the fill count matches.

Online checks download the same candles again from the exchange:
  5. Each fill's market price falls inside the high-low range the exchange
     reports for that minute (so the price was really tradable then).
  6. bars.csv matches the exchange's candles (the strategy saw real data).
     Coinbase's candle endpoint occasionally answers with slightly different
     numbers for the same minute, so a mismatched bar is fetched once more on
     its own before it counts. Small differences left on a few bars are a
     WARN; large or widespread differences are a FAIL.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .data.feed import make_feed
from .data.http import DataError

TOL_USD = 0.01
TOL_QTY = 1e-8
REVISION_PCT = 0.5        # a candle off by more than this is not a revision
REVISION_SHARE = 0.05     # nor is more than 5% of candles differing
MAX_REFETCH = 30


@dataclass
class Check:
    name: str
    passed: bool = True
    warned: bool = False
    notes: list[str] = field(default_factory=list)

    def fail(self, note: str) -> None:
        self.passed = False
        self.note(note)

    def warn(self, note: str) -> None:
        self.warned = True
        self.note(note)

    def note(self, note: str) -> None:
        if len(self.notes) < 10:
            self.notes.append(note)

    @property
    def label(self) -> str:
        return "FAIL" if not self.passed else "WARN" if self.warned else "PASS"


def _ts(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def ledger_checks(run_dir: Path) -> tuple[list[Check], dict]:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    cfg = run["config"]
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None
    trades = _rows(run_dir / "trades.csv")

    replay = Check("Ledger replay (cash, position, P&L)")
    fees = Check("Fees match the configured rate")
    slip = Check("Fill prices match market price + slippage")
    limits = Check("Risk limits respected")

    cash, qty, avg, realized, fee_total = float(cfg["account"]["starting_cash"]), 0.0, 0.0, 0.0, 0.0
    fee_bps, slip_bps = float(cfg["account"]["fee_bps"]), float(cfg["account"]["slippage_bps"])
    max_usd = float(cfg["risk"]["max_position_usd"])
    for i, t in enumerate(trades, 1):
        side, q, price, market = t["side"], float(t["qty"]), float(t["price"]), float(t["market_price"])
        fee = float(t["fee"])
        sign = 1 if side == "buy" else -1
        expected_fee = q * price * fee_bps / 10_000
        if abs(fee - expected_fee) > TOL_USD:
            fees.fail(f"fill {i}: fee ${fee:.4f}, expected ${expected_fee:.4f}")
        expected_price = round(market * (1 + sign * slip_bps / 10_000), 2)
        if abs(price - expected_price) > 0.011:
            slip.fail(f"fill {i}: price {price}, expected {expected_price} from market {market}")
        if qty and (qty > 0) != (sign > 0):
            realized += (price - avg) * min(q, abs(qty)) * (1 if qty > 0 else -1)
        new_qty = round(qty + sign * q, 8)
        if new_qty == 0:
            avg = 0.0
        elif qty == 0 or (qty > 0) != (new_qty > 0):
            avg = price
        elif (qty > 0) == (sign > 0):
            avg = (avg * abs(qty) + price * q) / abs(new_qty)
        if abs(new_qty) > abs(qty) and abs(new_qty) * market > max_usd * 1.0001:
            limits.fail(f"fill {i}: position ${abs(new_qty) * market:,.2f} over ${max_usd:,.2f}")
        cash = cash - sign * q * price - fee
        qty, fee_total = new_qty, fee_total + fee
        if abs(cash - float(t["cash_after"])) > TOL_USD:
            replay.fail(f"fill {i}: cash_after {t['cash_after']} but replay gives {cash:.2f}")
        if abs(qty - float(t["position_after"])) > TOL_QTY:
            replay.fail(f"fill {i}: position_after {t['position_after']} but replay gives {qty}")

    entries = sum(1 for t in trades if t["reason"].startswith("entry"))
    if entries > int(cfg["risk"]["max_trades_per_day"]):
        limits.fail(f"{entries} entries, limit {cfg['risk']['max_trades_per_day']}")

    if summary is None:
        replay.notes.append("no summary.json yet (run still going?); checked fills only")
    else:
        for key, value in (("final_cash", cash), ("final_position", qty), ("fees", fee_total),
                           ("realized_pnl", realized)):
            tol = TOL_QTY if key == "final_position" else TOL_USD
            if abs(float(summary[key]) - value) > tol:
                replay.fail(f"summary {key} = {summary[key]} but replay gives {value:.8f}")
        if summary.get("final_price") is not None:
            equity = cash + qty * float(summary["final_price"])
            if abs(float(summary["final_equity"]) - equity) > TOL_USD:
                replay.fail(f"summary final_equity {summary['final_equity']} but replay gives {equity:.2f}")
            if abs(float(summary["day_pnl"]) - (equity - float(cfg["account"]["starting_cash"]))) > TOL_USD:
                replay.fail("summary day_pnl does not equal final equity minus starting cash")
        if int(summary["fills"]) != len(trades):
            replay.fail(f"summary says {summary['fills']} fills, trades.csv has {len(trades)}")

    totals = {"fills": len(trades), "cash": cash, "position": qty, "realized": realized, "fees": fee_total,
              "summary": summary, "symbol": cfg["market"]["symbol"], "source": cfg["market"]["source"],
              "bar_seconds": int(cfg["market"]["bar_seconds"])}
    return [replay, fees, slip, limits], totals


def market_checks(run_dir: Path, totals: dict, feed=None) -> list[Check]:
    feed = feed or make_feed(totals["source"], totals["symbol"])
    trades = _rows(run_dir / "trades.csv")
    bars = _rows(run_dir / "bars.csv")
    prices = Check(f"Fill prices were real {totals['source']} prices at that minute")
    candles = Check(f"bars.csv matches {totals['source']} candles")

    for i, t in enumerate(trades, 1):
        when = _ts(t["quote_time"])
        minute = int(when // 60 * 60)
        try:
            around = feed.history(60, minute - 60, minute + 120)
        except DataError as exc:
            prices.fail(f"fill {i}: could not download candles ({exc})")
            continue
        if not around:
            prices.fail(f"fill {i}: exchange returned no candles near {t['quote_time']}")
            continue
        low, high = min(b.low for b in around), max(b.high for b in around)
        market = float(t["market_price"])
        if not low - 0.01 <= market <= high + 0.01:
            prices.fail(f"fill {i}: market price {market} outside {low}-{high} around {t['quote_time']}")
    if trades and prices.passed:
        prices.notes.append(f"{len(trades)} fill(s) checked against 1-minute candles (+/- 1 minute)")

    if bars:
        seconds = totals["bar_seconds"]
        start, end = int(bars[0]["start"]), int(bars[-1]["start"]) + seconds
        try:
            fetched = {b.start: b for b in feed.history(seconds, start, end)}
        except DataError as exc:
            candles.fail(f"could not download candles ({exc})")
            fetched = None
        if fetched is not None:
            def differs(row, bar):
                return any(abs(float(row[k]) - getattr(bar, k)) > 0.005 for k in ("open", "high", "low", "close"))

            retried = 0
            for row in bars:
                start_s = int(row["start"])
                bar = fetched.get(start_s)
                if bar is not None and differs(row, bar) and retried < MAX_REFETCH:
                    retried += 1
                    try:
                        again = {b.start: b for b in feed.history(seconds, start_s, start_s + seconds)}
                        fetched[start_s] = again.get(start_s, bar)
                    except DataError:
                        pass
            missing, diffs, worst, closes = 0, [], 0.0, 0
            for row in bars:
                bar = fetched.get(int(row["start"]))
                if bar is None:
                    missing += 1
                    continue
                for key in ("open", "high", "low", "close"):
                    logged, real = float(row[key]), getattr(bar, key)
                    if abs(logged - real) > 0.005:
                        pct = abs(logged - real) / real * 100
                        worst = max(worst, pct)
                        closes += key == "close"
                        diffs.append(f"{row['start_utc']} {key}: logged {logged}, exchange now {real} ({pct:.3f}%)")
            checked = len(bars) - missing
            bad_bars = len({d.split()[0] for d in diffs})
            if diffs and (worst > REVISION_PCT or bad_bars > max(1, checked * REVISION_SHARE)):
                for d in diffs:
                    candles.fail(d)
            elif diffs:
                candles.warn(f"{bad_bars} candle(s) differ slightly from what the exchange serves now, even after a re-fetch "
                             f"(largest change {worst:.3f}%; {closes} close price(s) changed)")
                for d in diffs:
                    candles.note(d)
            candles.note(f"{checked} of {len(bars)} bars compared")
            if missing:
                candles.note(f"{missing} bar(s) no longer served by the exchange (Kraken keeps ~720)")
    return [prices, candles]


def verify(run_dir: Path, online: bool = True, feed=None) -> tuple[bool, str]:
    run_dir = Path(run_dir)
    if not (run_dir / "run.json").exists():
        return False, f"{run_dir} is not a run folder (no run.json)"
    checks, totals = ledger_checks(run_dir)
    if online:
        checks += market_checks(run_dir, totals, feed)
    lines = [f"Verifying {run_dir}", ""]
    for check in checks:
        lines.append(f"[{check.label}] {check.name}")
        lines.extend(f"       {note}" for note in check.notes)
    s = totals["summary"]
    lines += ["", f"Fills: {totals['fills']}   Realized P&L: ${totals['realized']:+,.2f}   Fees: ${totals['fees']:,.2f}"]
    if s and s.get("final_price") is not None:
        lines.append(f"Day P&L (after fees, marked at ${s['final_price']:,.2f}): ${s['day_pnl']:+,.2f}")
    if not online:
        lines.append("(offline: exchange price checks skipped)")
    ok = all(c.passed for c in checks)
    if not ok:
        lines.append("Some checks FAILED - see above.")
    elif any(c.warned for c in checks):
        lines.append("All checks passed, with warnings - see above.")
    else:
        lines.append("All checks passed.")
    return ok, "\n".join(lines)
