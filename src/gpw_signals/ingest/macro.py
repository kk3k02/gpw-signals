"""Kalendarz makro i wyników spółek (plik YAML). Służy agentowi i filtrowi „wyniki tuż-tuż”."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from ..clock import MARKET_TZ, market_date, utc_now


class EventCalendar:
    def __init__(self, path: str | Path, tz: ZoneInfo = MARKET_TZ):
        self.tz = tz
        p = Path(path)
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        self.events: dict[date, list[str]] = {
            date.fromisoformat(str(k)): list(v or []) for k, v in (raw or {}).items()}

    def upcoming(self, days: int = 14, ticker: str | None = None, today: date | None = None) -> list[dict]:
        today = today or market_date(utc_now(), self.tz)
        out = []
        for d in sorted(self.events):
            if today <= d <= today + timedelta(days=days):
                for ev in self.events[d]:
                    is_company = ev.startswith("ticker:")
                    if is_company:
                        tk, _, desc = ev[len("ticker:"):].partition(" ")
                        if ticker and tk.upper() != ticker.upper():
                            continue
                        out.append({"date": d.isoformat(), "ticker": tk, "event": desc})
                    else:
                        out.append({"date": d.isoformat(), "ticker": None, "event": ev})
        return out

    def earnings_within(self, ticker: str, days: int = 3, today: date | None = None) -> bool:
        return any(e["ticker"] and "wynik" in e["event"].lower()
                   for e in self.upcoming(days, ticker, today))
