"""Jedno źródło czasu w aplikacji.

Zasady:
- w kodzie i w bazie trzymamy wyłącznie daty ze strefą (aware), zapisywane w UTC,
- "dzień" (limit sygnałów, kalendarz, sesja) liczymy w strefie rynku – Europe/Warsaw,
- nie używamy `datetime.now()` bez strefy ani `date.today()`: zależą od strefy serwera.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("Europe/Warsaw")


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime, naive_tz: ZoneInfo | None = None) -> datetime:
    """Normalizuje do UTC. Data bez strefy jest interpretowana w `naive_tz` (domyślnie UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz or UTC)
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    """Stały format zapisu w bazie (UTC, mikrosekundy) – porównania tekstowe w SQL są wtedy poprawne."""
    return to_utc(dt).isoformat(timespec="microseconds")


def from_iso(s: str) -> datetime:
    return to_utc(datetime.fromisoformat(s))


def market_date(dt: datetime, tz: ZoneInfo = MARKET_TZ) -> date:
    """Data w strefie rynku (np. sygnał z 00:30 czasu PL to już nowy dzień, choć w UTC jeszcze nie)."""
    return to_utc(dt).astimezone(tz).date()


def market_day_bounds(day: date, tz: ZoneInfo = MARKET_TZ) -> tuple[datetime, datetime]:
    """[początek, koniec) dnia `day` w strefie rynku, jako daty UTC. Uwzględnia zmianę czasu (23/25 h)."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)
