"""Economic calendar from Forex Factory's public weekly export.

Forex Factory has no official API. It does publish this week's calendar as a
JSON export (the same file its "export" button links to). The publisher asks
clients not to hammer it, so the bot caches the file and refetches at most
every few hours. The feed only covers the current week.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .http import DataError, get_json

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 3600


@dataclass(frozen=True)
class NewsEvent:
    title: str
    country: str
    time: float
    impact: str
    forecast: str = ""
    previous: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title, "country": self.country, "impact": self.impact,
            "time": datetime.fromtimestamp(self.time, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "forecast": self.forecast, "previous": self.previous,
        }


def parse_calendar(rows) -> list[NewsEvent]:
    events = []
    for row in rows or []:
        try:
            when = datetime.fromisoformat(row["date"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        events.append(NewsEvent(
            title=str(row.get("title", "")), country=str(row.get("country", "")), time=when,
            impact=str(row.get("impact", "")), forecast=str(row.get("forecast", "")),
            previous=str(row.get("previous", "")),
        ))
    return sorted(events, key=lambda e: e.time)


class NewsCalendar:
    def __init__(self, cfg: dict, cache_dir: Path, fetch=get_json, clock=time.time):
        self.enabled = bool(cfg.get("enabled", False))
        self.currencies = {c.upper() for c in cfg.get("currencies", ["USD"])}
        self.impacts = {i.lower() for i in cfg.get("impact", ["High"])}
        self.before = float(cfg.get("minutes_before", 15)) * 60
        self.after = float(cfg.get("minutes_after", 15)) * 60
        self.cache_file = Path(cache_dir) / "ff_calendar_thisweek.json"
        self._fetch = fetch
        self._clock = clock
        self.events: list[NewsEvent] = []
        self.fetched_at = 0.0
        self._next_try = 0.0
        self.error = ""

    def refresh(self, force: bool = False) -> None:
        if not self.enabled:
            return
        now = self._clock()
        if not self.events and self.cache_file.exists():
            try:
                cached = json.loads(self.cache_file.read_text(encoding="utf-8"))
                self.events = parse_calendar(cached["rows"])
                self.fetched_at = float(cached["fetched_at"])
                self._next_try = self.fetched_at + REFRESH_SECONDS
            except (OSError, ValueError, KeyError):
                pass
        if not force and now < self._next_try:
            return
        try:
            rows = self._fetch(FF_URL)
            self.events = parse_calendar(rows)
            self.fetched_at = now
            self._next_try = now + REFRESH_SECONDS
            self.error = ""
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps({"fetched_at": now, "rows": rows}), encoding="utf-8")
        except DataError as exc:
            self.error = str(exc)
            self._next_try = now + RETRY_SECONDS

    def relevant(self) -> list[NewsEvent]:
        return [e for e in self.events
                if e.country.upper() in self.currencies and e.impact.lower() in self.impacts]

    def blackout(self, now: float) -> NewsEvent | None:
        """The event whose no-new-trades window contains `now`, if any."""
        if not self.enabled:
            return None
        for event in self.relevant():
            if event.time - self.before <= now <= event.time + self.after:
                return event
        return None

    def upcoming(self, now: float, hours: float = 24) -> list[NewsEvent]:
        if not self.enabled:
            return []
        return [e for e in self.relevant() if now - self.after <= e.time <= now + hours * 3600]
