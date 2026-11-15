"""Load and check the backtest config (config/backtest.yaml, git-ignored)."""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from ..config import ROOT, TIME_RE, _merge, _number

EXAMPLE_PATH = ROOT / "config" / "backtest.example.yaml"
DEFAULT_PATH = ROOT / "config" / "backtest.yaml"

DEFAULTS: dict = {
    "data": {"file": "", "root": "MNQ", "contract": "", "start": "", "end": ""},
    "instrument": {"tick_size": 0.0, "point_value": 0.0},
    "bar_seconds": 60,
    "session": {"timezone": "America/New_York", "start": "09:30", "end": "15:55", "flatten_at_end": True},
    "account": {"starting_cash": 10000.0, "contracts": 1, "commission_per_contract": 0.62, "slippage_ticks": 1.0},
    "strategy": {"name": "sma_cross", "file": "", "params": {}},
    "exits": {"stop_loss_points": 0.0, "take_profit_points": 0.0, "target_fill": "through"},
    "risk": {"max_contracts": 1, "max_daily_loss_usd": 200.0, "max_trades_per_day": 10,
             "allow_short": True, "kill_switch": True},
}


def validate(raw: dict) -> tuple[dict, list[str]]:
    cfg = _merge(DEFAULTS, raw)
    errors: list[str] = []
    d = cfg["data"]
    d["root"] = str(d["root"] or "").strip().upper()
    d["contract"] = str(d["contract"] or "").strip().upper()
    if not d["root"]:
        errors.append("data.root is required, e.g. MNQ")
    if d["contract"] and not d["contract"].startswith(d["root"]):
        errors.append(f"data.contract {d['contract']} is not a {d['root']} contract")
    for key in ("start", "end"):
        if d[key]:
            try:
                date.fromisoformat(str(d[key]))
                d[key] = str(d[key])
            except ValueError:
                errors.append(f"data.{key} must be YYYY-MM-DD")
    _number(cfg["instrument"], "tick_size", errors, minimum=0, where="instrument")
    _number(cfg["instrument"], "point_value", errors, minimum=0, where="instrument")
    _number(cfg, "bar_seconds", errors, minimum=1, integer=True, where="(top level)")
    s = cfg["session"]
    for key in ("start", "end"):
        if not TIME_RE.match(str(s[key])):
            errors.append(f"session.{key} must be HH:MM (24-hour)")
    if s["start"] == s["end"]:
        errors.append("session.start and session.end are the same")
    try:
        ZoneInfo(str(s["timezone"]))
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"session.timezone {s['timezone']!r} is not a known time zone, e.g. America/New_York")
    a = cfg["account"]
    _number(a, "starting_cash", errors, minimum=1, where="account")
    _number(a, "contracts", errors, minimum=1, integer=True, where="account")
    _number(a, "commission_per_contract", errors, minimum=0, where="account")
    _number(a, "slippage_ticks", errors, minimum=0, where="account")
    e = cfg["exits"]
    _number(e, "stop_loss_points", errors, minimum=0, where="exits")
    _number(e, "take_profit_points", errors, minimum=0, where="exits")
    if e["target_fill"] not in ("through", "touch"):
        errors.append("exits.target_fill must be through or touch")
    r = cfg["risk"]
    _number(r, "max_contracts", errors, minimum=1, integer=True, where="risk")
    _number(r, "max_daily_loss_usd", errors, minimum=0, where="risk")
    _number(r, "max_trades_per_day", errors, minimum=1, integer=True, where="risk")
    if not errors and a["contracts"] > r["max_contracts"]:
        errors.append("account.contracts is larger than risk.max_contracts, so every entry would be refused")
    if not isinstance(cfg["strategy"].get("params") or {}, dict):
        errors.append("strategy.params must be a mapping, e.g. {fast: 9, slow: 21}")
    cfg["strategy"]["params"] = cfg["strategy"].get("params") or {}
    return cfg, errors


def ensure_local_config(path: Path = DEFAULT_PATH) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def load(path: Path = DEFAULT_PATH, overrides: dict | None = None) -> tuple[dict, list[str]]:
    """`overrides` (from the command line) win; their strategy.params merge into the file's."""
    raw = {}
    if Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULTS), [f"{path} must be a YAML mapping"]
    overrides = copy.deepcopy(overrides or {})
    wanted = overrides.pop("strategy", None) or {}
    cfg = _merge(raw, overrides)
    strategy = dict(cfg.get("strategy") or {})
    if wanted.get("name") and wanted["name"] != strategy.get("name"):
        strategy = {"name": wanted["name"], "file": "", "params": {}}  # the file's params belong to another strategy
    if wanted.get("file"):
        strategy["file"] = wanted["file"]
    strategy["params"] = {**(strategy.get("params") or {}), **(wanted.get("params") or {})}
    cfg["strategy"] = strategy
    return validate(cfg)
