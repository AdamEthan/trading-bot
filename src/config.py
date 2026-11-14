"""Load, validate, and save the local config file.

The committed file is config/config.example.yaml (placeholders only). The real
file, config/config.yaml, is created on first run and is git-ignored, so your
settings and strategy parameters stay on your computer.
"""
from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

import yaml

from .data.feed import BAR_SIZES, FEEDS

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = ROOT / "config" / "config.example.yaml"
DEFAULT_PATH = ROOT / "config" / "config.yaml"

DEFAULTS: dict = {
    "mode": "paper",
    "market": {"symbol": "BTC-USD", "source": "coinbase", "bar_seconds": 60, "poll_seconds": 10},
    "session": {"start": "now", "end": "23:59", "timezone": "", "flatten_at_end": True},
    "account": {"starting_cash": 10000.0, "position_size_usd": 1000.0, "fee_bps": 10.0, "slippage_bps": 2.0},
    "strategy": {"name": "sma_cross", "file": "", "params": {}},
    "exits": {"stop_loss_pct": 0.0, "take_profit_pct": 0.0},
    "risk": {"max_position_usd": 1000.0, "max_daily_loss_usd": 200.0, "max_trades_per_day": 10,
             "allow_short": False, "kill_switch": True, "max_data_age_seconds": 60},
    "news": {"enabled": True, "currencies": ["USD"], "impact": ["High"],
             "minutes_before": 15, "minutes_after": 15, "flatten_before": False},
    "dashboard": {"port": 8765, "open_browser": True},
}

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "params":
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _number(section: dict, key: str, errors: list, *, minimum=None, integer=False, where=""):
    try:
        value = int(section[key]) if integer else float(section[key])
    except (TypeError, ValueError):
        errors.append(f"{where}.{key} must be a number")
        return
    if minimum is not None and value < minimum:
        errors.append(f"{where}.{key} must be at least {minimum}")
    section[key] = value


def validate(cfg: dict) -> tuple[dict, list[str]]:
    """Return (normalised config, list of problems). Empty list means valid."""
    cfg = _merge(DEFAULTS, cfg)
    errors: list[str] = []
    if cfg["mode"] != "paper":
        errors.append("mode must be 'paper' (live trading is not built)")

    m = cfg["market"]
    m["symbol"] = str(m["symbol"]).upper()
    if not re.match(r"^[A-Z0-9]+-[A-Z0-9]+$", m["symbol"]):
        errors.append("market.symbol must look like BTC-USD")
    if m["source"] not in FEEDS:
        errors.append(f"market.source must be one of {', '.join(FEEDS)}")
    _number(m, "bar_seconds", errors, integer=True, where="market")
    if m.get("bar_seconds") not in BAR_SIZES:
        errors.append(f"market.bar_seconds must be one of {', '.join(map(str, BAR_SIZES))}")
    _number(m, "poll_seconds", errors, minimum=2, where="market")

    s = cfg["session"]
    s["start"], s["end"] = str(s["start"]).strip(), str(s["end"]).strip()
    if s["start"] != "now" and not TIME_RE.match(s["start"]):
        errors.append("session.start must be 'now' or HH:MM (24-hour)")
    if not TIME_RE.match(s["end"]):
        errors.append("session.end must be HH:MM (24-hour)")
    s["timezone"] = str(s.get("timezone") or "").strip()
    if s["timezone"]:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(s["timezone"])
        except Exception:
            errors.append(f"session.timezone {s['timezone']!r} is not a known time zone (try America/New_York)")
    s["flatten_at_end"] = bool(s["flatten_at_end"])

    a = cfg["account"]
    for key in ("starting_cash", "position_size_usd"):
        _number(a, key, errors, minimum=1, where="account")
    for key in ("fee_bps", "slippage_bps"):
        _number(a, key, errors, minimum=0, where="account")

    e = cfg["exits"]
    for key in ("stop_loss_pct", "take_profit_pct"):
        _number(e, key, errors, minimum=0, where="exits")

    r = cfg["risk"]
    _number(r, "max_position_usd", errors, minimum=1, where="risk")
    _number(r, "max_daily_loss_usd", errors, minimum=0, where="risk")
    _number(r, "max_trades_per_day", errors, minimum=1, integer=True, where="risk")
    _number(r, "max_data_age_seconds", errors, minimum=5, where="risk")
    r["allow_short"], r["kill_switch"] = bool(r["allow_short"]), bool(r["kill_switch"])
    if not errors and a["position_size_usd"] > r["max_position_usd"]:
        errors.append("account.position_size_usd is larger than risk.max_position_usd, so every entry would be refused")

    n = cfg["news"]
    n["enabled"], n["flatten_before"] = bool(n["enabled"]), bool(n["flatten_before"])
    for key in ("currencies", "impact"):
        if isinstance(n[key], str):
            n[key] = [x.strip() for x in n[key].split(",") if x.strip()]
    for key in ("minutes_before", "minutes_after"):
        _number(n, key, errors, minimum=0, where="news")

    d = cfg["dashboard"]
    _number(d, "port", errors, minimum=1024, integer=True, where="dashboard")
    d["open_browser"] = bool(d["open_browser"])

    st = cfg["strategy"]
    st["file"] = str(st.get("file") or "")
    st["params"] = st.get("params") or {}
    if not errors:
        from .strategy.registry import build_strategy
        try:
            build_strategy(st, ROOT)
        except Exception as exc:
            errors.append(f"strategy: {exc}")
    return cfg, errors


def ensure_local_config(path: Path = DEFAULT_PATH) -> bool:
    """Create the local config from the example on first run. True if created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def load(path: Path = DEFAULT_PATH) -> tuple[dict, list[str]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULTS), [f"{path} must be a YAML mapping"]
    return validate(raw)


HEADER = """\
# Local bot settings. This file is git-ignored: it stays on this computer.
# Edit by hand or at the settings page (python run.py settings).
# Saving from the settings page rewrites this file without comments;
# config/config.example.yaml documents every field.
"""


def save(cfg: dict, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(HEADER + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False, allow_unicode=True),
                   encoding="utf-8")
    tmp.replace(path)


def git_privacy(path: Path) -> dict:
    """Is this path safe from `git add`? Used for the startup check and the settings page."""
    path = Path(path).resolve()
    try:
        inside = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {"status": "no-git", "message": "git not installed; file is only on this computer"}
    if inside.returncode != 0:
        return {"status": "outside-repo", "message": "not inside a git repository; file is only on this computer"}
    ignored = subprocess.run(["git", "-C", str(path.parent), "check-ignore", "-q", str(path)],
                             capture_output=True, timeout=5)
    if ignored.returncode == 0:
        tracked = subprocess.run(["git", "-C", str(path.parent), "ls-files", "--error-unmatch", str(path)],
                                 capture_output=True, timeout=5)
        if tracked.returncode == 0:
            return {"status": "tracked", "message": "WARNING: ignored but already committed to git. Run: git rm --cached " + path.name}
        return {"status": "ignored", "message": "git-ignored; never committed or pushed"}
    return {"status": "exposed", "message": "WARNING: git would commit this file. Add it to .gitignore."}
