"""Find a strategy by name, or load one from a private file on disk."""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path

from .example import ExampleStrategy
from .interface import Strategy
from .rsi_reversion import RsiReversion
from .sma_cross import SmaCross

BUILTINS: dict[str, type[Strategy]] = {cls.name: cls for cls in (SmaCross, RsiReversion, ExampleStrategy)}
CUSTOM = "custom"


def resolve_path(file: str, root: Path) -> Path:
    path = Path(file).expanduser()
    return path if path.is_absolute() else (root / path)


def load_class_from_file(path: Path) -> type[Strategy]:
    if not path.is_file():
        raise ValueError(f"Strategy file not found: {path}")
    module_name = f"private_strategy_{hashlib.sha1(str(path).encode()).hexdigest()[:10]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    found = [obj for _, obj in inspect.getmembers(module, inspect.isclass)
             if issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module_name]
    if len(found) != 1:
        raise ValueError(f"{path} must define exactly one Strategy subclass (found {len(found)})")
    return found[0]


def strategy_class(cfg: dict, root: Path) -> type[Strategy]:
    name = cfg.get("name", "")
    if name == CUSTOM:
        if not cfg.get("file"):
            raise ValueError("strategy.name is 'custom' but strategy.file is empty")
        return load_class_from_file(resolve_path(cfg["file"], root))
    try:
        return BUILTINS[name]
    except KeyError:
        raise ValueError(f"Unknown strategy {name!r}. Built-ins: {', '.join(BUILTINS)}, or 'custom'") from None


def build_strategy(cfg: dict, root: Path) -> Strategy:
    return strategy_class(cfg, root)(**(cfg.get("params") or {}))


def file_fingerprint(cfg: dict, root: Path) -> str | None:
    """SHA-256 of a custom strategy file, logged so a run can be tied to exact code."""
    if cfg.get("name") != CUSTOM or not cfg.get("file"):
        return None
    path = resolve_path(cfg["file"], root)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def catalog(cfg: dict, root: Path) -> list[dict]:
    """Every selectable strategy and its parameter schema, for the settings page."""
    entries = [{"name": cls.name, "description": cls.description,
                "params": {k: p.to_dict() for k, p in cls.params.items()}} for cls in BUILTINS.values()]
    custom = {"name": CUSTOM, "description": "Your own private .py file.", "params": {}, "error": ""}
    if cfg.get("file"):
        try:
            cls = load_class_from_file(resolve_path(cfg["file"], root))
            custom.update(description=f"{cls.name}: {cls.description}",
                          params={k: p.to_dict() for k, p in cls.params.items()})
        except Exception as exc:  # show any import problem in the form instead of crashing
            custom["error"] = str(exc)
    entries.append(custom)
    return entries
