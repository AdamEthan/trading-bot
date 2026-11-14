#!/usr/bin/env python3
"""Bitcoin paper-trading bot. Start here.

  python run.py                     trade today's session (paper money only)
  python run.py run --minutes 30    a short test run
  python run.py settings            open the settings page without trading
  python run.py dashboard           browse past runs
  python run.py verify logs/<run>   recheck a run's trades against the exchange
  python run.py check               test the data feeds and the privacy setup
"""
from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or newer is required.")

ROOT = Path(__file__).resolve().parent
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace")  # Windows consoles cannot print every character
    except (AttributeError, ValueError):
        pass
sys.path.insert(0, str(ROOT))

try:
    from src import config as config_mod
except ModuleNotFoundError as exc:
    sys.exit(f"Missing package: {exc.name}. Run:  pip install -r requirements.txt")

LOGS = ROOT / "logs"


def load_config(path: Path) -> dict:
    if config_mod.ensure_local_config(path):
        print(f"Created {path} from the example. Edit it, or run:  python run.py settings")
    cfg, errors = config_mod.load(path)
    if errors:
        print(f"Problems in {path}:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    privacy = config_mod.git_privacy(path)
    print(f"Config: {path} ({privacy['message']})")
    if privacy["status"] in ("exposed", "tracked"):
        print("  Your settings could end up on GitHub. Fix this before sharing the repo.")
    return cfg


def open_dashboard(url: str, enabled: bool) -> None:
    print(f"Dashboard: {url}")
    if enabled:
        try:
            webbrowser.open(url)
        except Exception:
            pass


def cmd_run(args) -> int:
    from src.engine import Engine
    from src.web.server import App, serve

    cfg = load_config(args.config)
    run_dir = LOGS / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    engine = Engine(cfg, run_dir, minutes=args.minutes, config_path=args.config)
    if engine.end_ts <= time.time():
        print(f"The session already ended today (session.end = {cfg['session']['end']}). "
              "Change it in the settings, or run a test with:  python run.py run --minutes 60")
        return 1
    _, port = serve(App(LOGS, args.config, live_run=run_dir), cfg["dashboard"]["port"])
    open_dashboard(f"http://127.0.0.1:{port}/", cfg["dashboard"]["open_browser"] and not args.no_browser)
    print("Press Ctrl+C to stop early (the bot closes its paper position first).\n")
    engine.run()
    print(f"\nDashboard stays up for review at http://127.0.0.1:{port}/ - press Ctrl+C to exit.")
    try:
        while not args.exit_when_done:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_serve(args, page: str) -> int:
    from src.web.server import App, serve

    config_mod.ensure_local_config(args.config)
    cfg, _ = config_mod.load(args.config)
    server, port = serve(App(LOGS, args.config), cfg["dashboard"]["port"], background=False)
    open_dashboard(f"http://127.0.0.1:{port}/{page}", not args.no_browser)
    print("Press Ctrl+C to exit.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_verify(args) -> int:
    from src.verify import verify

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute() and not run_dir.exists():
        run_dir = LOGS / run_dir
    ok, report = verify(run_dir, online=not args.offline)
    print(report)
    return 0 if ok else 2


def cmd_check(args) -> int:
    from src.data.feed import FEEDS
    from src.data.http import DataError
    from src.data.news import NewsCalendar

    cfg = load_config(args.config)
    symbol = cfg["market"]["symbol"]
    ok = True
    for name, feed_cls in FEEDS.items():
        try:
            quote = feed_cls(symbol).quote()
            bars = feed_cls(symbol).candles(60)
            print(f"[ OK ] {name}: {symbol} ${quote.price:,.2f}, {len(bars)} recent 1-minute candles")
        except DataError as exc:
            ok = ok and name != cfg["market"]["source"]
            print(f"[FAIL] {name}: {exc}")
    news = NewsCalendar({**cfg["news"], "enabled": True}, ROOT / "data")
    news.refresh()  # uses the cached copy if it is recent; Forex Factory rate-limits repeat downloads
    if news.error and news.events:
        print(f"[WARN] Forex Factory calendar: {news.error}; using the cached copy "
              f"({len(news.events)} events)")
    elif news.error:
        print(f"[FAIL] Forex Factory calendar: {news.error}")
    else:
        print(f"[ OK ] Forex Factory calendar: {len(news.events)} events this week, "
              f"{len(news.relevant())} match your filter")
    for path in (args.config, ROOT / "logs" / "any-run" / "trades.csv",
                 ROOT / "src" / "strategy" / "private" / "my_strategy.py"):
        privacy = config_mod.git_privacy(path)
        mark = " OK " if privacy["status"] in ("ignored", "outside-repo", "no-git") else "WARN"
        print(f"[{mark}] {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: {privacy['message']}")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bitcoin paper-trading bot")
    parser.add_argument("--config", type=Path, default=config_mod.DEFAULT_PATH,
                        help="config file (default: config/config.yaml)")
    sub = parser.add_subparsers(dest="command")
    p_run = sub.add_parser("run", help="trade today's session on paper (default)")
    p_run.add_argument("--minutes", type=float, help="run for this many minutes instead of the session times")
    p_run.add_argument("--no-browser", action="store_true", help="do not open the dashboard")
    p_run.add_argument("--exit-when-done", action="store_true", help="exit when the session ends")
    for name in ("settings", "dashboard"):
        p = sub.add_parser(name, help=f"open the {name} page without trading")
        p.add_argument("--no-browser", action="store_true")
    p_verify = sub.add_parser("verify", help="recheck a run's logs")
    p_verify.add_argument("run_dir", help="folder under logs/, e.g. logs/2026-11-14_090000")
    p_verify.add_argument("--offline", action="store_true", help="skip the exchange price checks")
    sub.add_parser("check", help="test data feeds and privacy setup")

    args = parser.parse_args(argv)
    if args.command in (None, "run"):
        if args.command is None:
            args.minutes, args.no_browser, args.exit_when_done = None, False, False
        return cmd_run(args)
    if args.command == "settings":
        return cmd_serve(args, "settings")
    if args.command == "dashboard":
        return cmd_serve(args, "")
    if args.command == "verify":
        return cmd_verify(args)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
