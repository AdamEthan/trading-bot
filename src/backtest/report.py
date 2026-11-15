"""report.html for a backtest: one self-contained page, no scripts, works offline."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--up:#1f7a4d;--down:#b3261e;--accent:#2f5fb3;--dd:rgba(179,38,30,.14)}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--fg:#ecebe6;--muted:#a09f99;--line:#33332f;--up:#4fbf86;--down:#ef6b61;--accent:#7aa2f7;--dd:rgba(239,107,97,.18)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px}
.sub{color:var(--muted);margin:0 0 18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.tile b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
.tile span{color:var(--muted);font-size:12px}.up{color:var(--up)}.down{color:var(--down)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px}td:first-child,th:first-child{text-align:left}td.l,th.l{text-align:left}
svg{display:block;width:100%;height:auto}.note{color:var(--muted);font-size:12px;margin-top:8px}
"""


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _money(x, sign=True) -> str:
    if x is None or x == "":
        return "-"
    x = float(x)
    return f"{'+' if sign and x > 0 else ''}{'-' if x < 0 else ''}${abs(x):,.2f}"


def _cls(x) -> str:
    try:
        return "up" if float(x) > 0 else "down" if float(x) < 0 else ""
    except (TypeError, ValueError):
        return ""


def _chart(equity: list[dict], start_cash: float) -> str:
    if len(equity) < 2:
        return "<p class='note'>Not enough bars to draw a chart.</p>"
    step = max(1, len(equity) // 1500)
    pts = equity[::step] + ([equity[-1]] if (len(equity) - 1) % step else [])
    vals = [float(r["equity"]) for r in pts]
    dds = [float(r["drawdown"]) for r in pts]
    w, h, pad_l, pad_r, pad_t, pad_b = 1000, 300, 70, 10, 10, 24
    lo, hi = min(vals + [start_cash]), max(vals + [start_cash])
    if hi - lo < 1:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    lo, hi = lo - span * 0.05, hi + span * 0.05

    def x(i):
        return pad_l + (w - pad_l - pad_r) * i / (len(pts) - 1)

    def y(v):
        return pad_t + (h - pad_t - pad_b) * (hi - v) / (hi - lo)

    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    dd_area = (f"{x(0):.1f},{y(vals[0] + dds[0]):.1f} "
               + " ".join(f"{x(i):.1f},{y(v + d):.1f}" for i, (v, d) in enumerate(zip(vals, dds)))
               + " " + " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in reversed(list(enumerate(vals)))))
    grid = []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        grid.append(f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='var(--line)'/>"
                    f"<text x='{pad_l - 6}' y='{y(v) + 4:.1f}' text-anchor='end' font-size='11' fill='var(--muted)'>"
                    f"${v:,.0f}</text>")
    labels = []
    for i in (0, len(pts) // 2, len(pts) - 1):
        anchor = "start" if i == 0 else "end" if i == len(pts) - 1 else "middle"
        labels.append(f"<text x='{x(i):.1f}' y='{h - 6}' text-anchor='{anchor}' font-size='11' fill='var(--muted)'>"
                      f"{html.escape(pts[i]['time'][:10])}</text>")
    base = (f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{y(start_cash):.1f}' y2='{y(start_cash):.1f}' "
            f"stroke='var(--muted)' stroke-dasharray='4 4'/>")
    return (f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='Equity curve'>{''.join(grid)}{base}"
            f"<polygon points='{dd_area}' fill='var(--dd)'/>"
            f"<polyline points='{line}' fill='none' stroke='var(--accent)' stroke-width='1.6'/>{''.join(labels)}</svg>"
            "<p class='note'>Blue: account equity at each bar close, open position marked to market. "
            "Red band: drawdown from the running peak. Dashed: starting cash. Gaps between sessions are not to scale.</p>")


def write_report(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    s = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    equity = _rows(run_dir / "equity.csv")
    daily = _rows(run_dir / "daily.csv")
    trips = _rows(run_dir / "round_trips.csv")
    cfg = run["config"]
    a, sess = cfg["account"], cfg["session"]

    def tile(label, value, cls=""):
        return f"<div class='tile'><b class='{cls}'>{html.escape(str(value))}</b><span>{html.escape(label)}</span></div>"

    pct = lambda v: "-" if v is None else f"{v:.1%}"  # noqa: E731
    num = lambda v, f="{:.2f}": "-" if v is None else f.format(v)  # noqa: E731
    tiles = "".join([
        tile("Net P&L", _money(s["net_pnl"]), _cls(s["net_pnl"])),
        tile("Round trips", s["round_trips"]),
        tile("Win rate", pct(s["win_rate"])),
        tile("Profit factor", num(s["profit_factor"])),
        tile("Expectancy / trade", _money(s["expectancy_per_trade"]), _cls(s["expectancy_per_trade"])),
        tile("Max drawdown", _money(-s["max_drawdown"]) if s["max_drawdown"] else "$0.00", "down" if s["max_drawdown"] else ""),
        tile("Commissions", _money(s["commissions"], sign=False)),
        tile("Sessions up", f"{s['winning_sessions']} / {s['sessions']}"),
    ])
    day_rows = "".join(
        f"<tr><td>{html.escape(d['session'])}</td><td class='l'>{html.escape(d['symbol'])}</td><td>{d['trades']}</td>"
        f"<td>{d['wins']}</td><td class='{_cls(d['gross_pnl'])}'>{_money(d['gross_pnl'])}</td>"
        f"<td>{_money(d['commission'], sign=False)}</td><td class='{_cls(d['net_pnl'])}'>{_money(d['net_pnl'])}</td>"
        f"<td>{_money(d['equity_end'], sign=False)}</td><td class='l'>{html.escape(d['halted'])}</td></tr>"
        for d in daily)
    shown = trips[-500:]
    trip_rows = "".join(
        f"<tr><td>{t['trade']}</td><td class='l'>{html.escape(t['side'])}</td><td>{t['qty']}</td>"
        f"<td class='l'>{html.escape(t['entry_time'])}</td><td>{float(t['entry_price']):,.2f}</td>"
        f"<td class='l'>{html.escape(t['exit_time'])}</td><td>{float(t['exit_price']):,.2f}</td>"
        f"<td class='{_cls(t['points'])}'>{float(t['points']):+.2f}</td>"
        f"<td class='{_cls(t['net_pnl'])}'>{_money(t['net_pnl'])}</td><td class='l'>{html.escape(t['exit_reason'])}</td></tr>"
        for t in shown)
    more = f"<p class='note'>Showing the last {len(shown)} of {len(trips)} trades; all are in round_trips.csv.</p>" \
        if len(trips) > len(shown) else ""
    params = ", ".join(f"{k}={v}" for k, v in s["strategy_params"].items()) or "defaults"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Backtest {html.escape(s['run_id'])}</title>
<style>{CSS}</style></head><body><main>
<h1>Backtest: {html.escape(s['strategy'])} on {html.escape(s['root'])}</h1>
<p class="sub">{html.escape(params)} · {s['bar_seconds']}s bars · session {html.escape(sess['start'])}–{html.escape(sess['end'])} {html.escape(sess['timezone'])}
· sessions {html.escape(str(s.get('first_session') or '-'))} to {html.escape(str(s.get('last_session') or '-'))} · {html.escape(', '.join(s['contracts_traded']) or 'no trades')}<br>
{a['contracts']} contract(s), ${a['commission_per_contract']:.2f}/contract/side commission, {a['slippage_ticks']:g} tick(s) slippage on market and stop fills.
Data file {html.escape(run['data_file'])} (sha256 {html.escape(run['data_sha256'][:12])}…), code {html.escape(run['code_commit'])}.</p>
<div class="grid">{tiles}</div>
<h2>Equity</h2><div class="card">{_chart(equity, float(s['starting_cash']))}</div>
<h2>Sessions</h2><div class="card"><table><thead><tr><th>Session</th><th class='l'>Contract</th><th>Trades</th><th>Wins</th>
<th>Gross</th><th>Commission</th><th>Net</th><th>Equity</th><th class='l'>Halted</th></tr></thead><tbody>{day_rows}</tbody></table></div>
<h2>Trades</h2><div class="card"><table><thead><tr><th>#</th><th class='l'>Side</th><th>Qty</th><th class='l'>Entry (UTC)</th>
<th>Price</th><th class='l'>Exit (UTC)</th><th>Price</th><th>Points</th><th>Net</th><th class='l'>Exit reason</th></tr></thead>
<tbody>{trip_rows}</tbody></table>{more}</div>
<p class="note">Simulated results on historical data. They leave out queue position, partial fills, outages and
changing fees, and a rule tuned on this data will look better here than it will trade. Recheck with
<code>python run.py verify {html.escape(str(run_dir.as_posix()))}</code>.</p>
</main></body></html>"""
    out = run_dir / "report.html"
    out.write_text(page, encoding="utf-8")
    return out
