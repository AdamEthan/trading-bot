"""Re-check a backtest's logs against the data file it ran on.

  1. The data file is the same one (SHA-256 matches run.json).
  2. Replaying trades.csv from the starting cash reproduces every position,
     average price, realized P&L, commission and cash column, and the totals
     in summary.json, round_trips.csv and daily.csv.
  3. Every fill price is the recorded market price worsened by the slippage
     (targets: no slippage), on the tick grid.
  4. Every market price is really in the data, for that contract and that
     second: the candle's open (decisions) or close (forced exits), or inside
     its high-low range (stops and targets).
  5. No look-ahead: a decision made at a bar close filled at the first candle
     at or after that time, and never before it.
  6. Each fill used the contract the roll rule picks for that trade date.
  7. Running the backtest again with the same config and data gives exactly
     the same fills (skipped with --no-replay, or if a custom strategy file
     has changed since).
"""
from __future__ import annotations

import csv
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..brokers.sim_futures import Instrument
from ..data import databento
from ..strategy.registry import file_fingerprint
from ..verify import Check

TOL = 0.005


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _ts(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def _close(a: float, b: float, tol: float = TOL) -> bool:
    return abs(float(a) - float(b)) <= tol


def ledger_check(run: dict, summary: dict, fills: list[dict], trips: list[dict], daily: list[dict]) -> list[Check]:
    cfg = run["config"]
    inst = Instrument(**run["instrument"])
    rate, slip = float(cfg["account"]["commission_per_contract"]), float(cfg["account"]["slippage_ticks"])
    ledger = Check("Ledger replay: positions, P&L, commission and cash in trades.csv")
    prices = Check("Fill prices = market price worsened by the slippage, on the tick grid")
    size = Check("No position larger than risk.max_contracts")
    cash, pos, avg = float(cfg["account"]["starting_cash"]), 0, 0.0
    realized = commissions = 0.0
    for f in fills:
        fid, qty, side, price = f["fill_id"], int(f["qty"]), f["side"], float(f["price"])
        market = float(f["market_price"])
        sign = 1 if side == "buy" else -1
        expect = market if f["basis"] == "target" else market + sign * slip * inst.tick_size
        if not _close(price, inst.round_price(expect)):
            prices.fail(f"{fid}: price {price} but market {market} {'+' if sign > 0 else '-'} {slip:g} ticks = {expect}")
        if not _close(price / inst.tick_size, round(price / inst.tick_size), 1e-6):
            prices.fail(f"{fid}: price {price} is not on the {inst.tick_size} tick grid")
        signed = sign * qty
        r = 0.0
        if pos and (pos > 0) != (signed > 0):
            r = (price - avg) * min(qty, abs(pos)) * (1 if pos > 0 else -1) * inst.point_value
        new = pos + signed
        avg = 0.0 if new == 0 else price if pos == 0 or (pos > 0) != (new > 0) else \
            (avg * abs(pos) + price * qty) / abs(new) if (pos > 0) == (signed > 0) else avg
        pos = new
        c = round(qty * rate, 2)
        cash = round(cash + r - c, 2)
        realized += r
        commissions += c
        for name, want, got in (("realized_pnl", r, f["realized_pnl"]), ("commission", c, f["commission"]),
                                ("position_after", pos, f["position_after"]), ("avg_price_after", avg, f["avg_price_after"]),
                                ("cash_after", cash, f["cash_after"])):
            if not _close(want, got, 0.011):
                ledger.fail(f"{fid}: {name} is {got}, replay gives {round(want, 6)}")
        if abs(pos) > int(cfg["risk"]["max_contracts"]):
            size.fail(f"{fid}: position {pos} over the limit of {cfg['risk']['max_contracts']}")
    totals = Check("summary.json, round_trips.csv and daily.csv agree with the ledger")
    for name, want in (("final_equity", cash), ("net_pnl", cash - float(cfg["account"]["starting_cash"])),
                       ("gross_pnl", realized), ("commissions", commissions), ("fills", len(fills)),
                       ("round_trips", len(trips)), ("final_position", pos)):
        if not _close(want, summary.get(name, float("nan")), 0.02):
            totals.fail(f"summary {name} is {summary.get(name)}, ledger gives {round(want, 2)}")
    if pos:
        totals.fail(f"the ledger ends holding {pos} contracts; a backtest should end flat")
    if not _close(sum(float(t["net_pnl"]) for t in trips), summary["net_pnl"], 0.02 + 0.01 * len(trips)):
        totals.fail("round_trips.csv net P&L does not add up to summary net_pnl")
    if daily:
        start = float(cfg["account"]["starting_cash"])
        if not _close(sum(float(d["net_pnl"]) for d in daily), float(daily[-1]["equity_end"]) - start,
                      0.02 + 0.01 * len(daily)):
            totals.fail("daily.csv net P&L does not add up to the change in equity_end")
    return [ledger, prices, size, totals]


def data_checks(data: Path, run: dict, fills: list[dict]) -> list[Check]:
    real = Check("Every market price is in the data for that contract and second")
    ahead = Check("No look-ahead: decisions filled at the first price after they were made")
    roll = Check("Each fill used the contract the roll rule picks for that trade date")
    plan = databento.plan_contracts(data, run["instrument"]["root"], run["config"]["data"]["contract"] or None)
    recorded = run.get("contracts", {}).get("by_trade_date", {})
    if recorded != plan.to_dict()["by_trade_date"]:
        roll.fail("the contract per trade date in run.json differs from the data")
    td = databento.TradeDates()
    by_time: dict[tuple[str, int], list[dict]] = {}
    for f in fills:
        by_time.setdefault((f["symbol"], _ts(f["source_time"])), []).append(f)
        if plan.symbol_for(td(_ts(f["source_time"]))) != f["symbol"]:
            roll.fail(f"{f['fill_id']}: traded {f['symbol']} on trade date {td(_ts(f['source_time']))}, "
                      f"but the front month then was {plan.symbol_for(td(_ts(f['source_time'])))}")
    symbols = {f["symbol"] for f in fills}
    step = int(run.get("source_seconds", 1))
    last_seen: dict[str, int] = {}
    found = 0
    for r in databento.iter_rows(data, symbols):
        if plan.symbol_for(td(r.ts)) != r.symbol:
            continue
        for f in by_time.get((r.symbol, r.ts), []):
            found += 1
            m, basis, fid = float(f["market_price"]), f["basis"], f["fill_id"]
            if basis == "open" and not _close(m, r.open):
                real.fail(f"{fid}: market price {m} but the {f['source_time']} open was {r.open}")
            elif basis == "close" and not _close(m, r.close):
                real.fail(f"{fid}: market price {m} but the {f['source_time']} close was {r.close}")
            elif basis in ("stop", "target") and not (r.low - TOL <= m <= r.high + TOL):
                real.fail(f"{fid}: {basis} price {m} is outside {f['source_time']}'s range {r.low}-{r.high}")
            decided = _ts(f["decided_at"])
            if basis == "open":  # a decision: the first candle at or after it
                if r.ts < decided:
                    ahead.fail(f"{fid}: filled from {f['source_time']}, before the decision at {f['decided_at']}")
                elif last_seen.get(r.symbol, -1) >= decided:
                    ahead.fail(f"{fid}: decided at {f['decided_at']} but skipped the candle at "
                               f"{datetime.fromtimestamp(last_seen[r.symbol], timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
            elif basis == "close" and r.ts + step > decided:  # a forced exit at the last close already known
                ahead.fail(f"{fid}: exit decided at {f['decided_at']} used the close of {f['source_time']}, "
                           "which was not finished yet")
            elif basis in ("stop", "target") and r.ts != decided:
                ahead.fail(f"{fid}: {basis} decided at {f['decided_at']} but filled from {f['source_time']}")
        last_seen[r.symbol] = r.ts
    if found != len(fills):
        real.fail(f"{len(fills) - found} fill(s) point at a second with no candle for that contract in the data")
    return [real, ahead, roll]


def replay_check(run_dir: Path, run: dict, data: Path, root_dir: Path) -> Check:
    from .runner import Backtest
    check = Check("Running the backtest again gives the same fills")
    cfg = run["config"]
    if cfg["strategy"].get("name") == "custom":
        now = file_fingerprint(cfg["strategy"], root_dir)
        if now != run.get("strategy_file_sha256"):
            check.warn("skipped: the custom strategy file has changed (or is missing) since this run")
            return check
    with tempfile.TemporaryDirectory() as tmp:
        again = Backtest(cfg, data, Path(tmp) / run["run_id"], root_dir=root_dir, quiet=True).run()
        old = _rows(run_dir / "trades.csv")
        new = [{k: str(v) for k, v in f.row().items()} for f in again.fills]
        keys = [k for k in (old[0] if old else {}) if k not in ("fill_id", "order_id")]
        if len(old) != len(new):
            check.fail(f"{len(old)} fills logged, {len(new)} on the rerun")
        for a, b in zip(old, new):
            diff = [k for k in keys if not (a[k] == b[k] or _num_eq(a[k], b[k]))]
            if diff:
                check.fail(f"{a['fill_id']}: {', '.join(diff)} differ on the rerun")
    return check


def _num_eq(a: str, b: str) -> bool:
    try:
        return _close(float(a), float(b), 1e-6)
    except ValueError:
        return False


def _recorded_hashes(run: dict) -> list[dict]:
    if run.get("data_files"):
        return run["data_files"]
    return [{"file": run["data_file"], "sha256": run["data_sha256"]}]  # runs from before multi-file support


def verify_backtest(run_dir: Path, data=None, *, replay: bool = True,
                    root_dir: Path | None = None) -> tuple[bool, str]:
    from ..config import ROOT
    root_dir = root_dir or ROOT
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return False, f"{run_dir} has no summary.json; the backtest did not finish"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    fills, trips, daily = (_rows(run_dir / n) for n in ("trades.csv", "round_trips.csv", "daily.csv"))
    checks = ledger_check(run, summary, fills, trips, daily)
    lines = [f"Verifying backtest {run_dir}", ""]
    source = Check("Data files are the ones the backtest ran on")
    wanted = _recorded_hashes(run)
    try:
        data = databento.as_dataset(data or run["config"]["data"]["file"])
        given = data.hashes()
    except databento.DataFileError as exc:
        data, given = None, []
        source.fail(f"{exc}. Pass the data with --data")
    if data is not None and sorted(h["sha256"] for h in given) != sorted(h["sha256"] for h in wanted):
        known = {h["sha256"] for h in wanted}
        extra = [h["file"] for h in given if h["sha256"] not in known]
        missing = [h["file"] for h in wanted if h["sha256"] not in {g["sha256"] for g in given}]
        if extra:
            source.fail(f"not used by this backtest (different SHA-256): {', '.join(extra)}")
        if missing:
            source.fail(f"missing, or changed since the backtest: {', '.join(missing)}")
        if not extra and not missing:
            source.fail("the same file was passed more than once")
    if data is None or not source.passed:
        checks.append(source)
    else:
        source.note(f"{data.label}, sha256 {run['data_sha256'][:16]}…")
        checks.append(source)
        checks += data_checks(data, run, fills)
        if replay:
            checks.append(replay_check(run_dir, run, data, root_dir))
    for check in checks:
        lines.append(f"[{check.label}] {check.name}")
        lines.extend(f"       {note}" for note in check.notes)
    lines += ["", f"Fills: {len(fills)}   Round trips: {len(trips)}   Net P&L: ${summary['net_pnl']:+,.2f}   "
                  f"Commissions: ${summary['commissions']:,.2f}"]
    ok = all(c.passed for c in checks)
    if not ok:
        lines.append("Some checks FAILED - see above.")
    elif any(c.warned for c in checks):
        lines.append("All checks passed, with warnings - see above.")
    else:
        lines.append("All checks passed.")
    return ok, "\n".join(lines)
