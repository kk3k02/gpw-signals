"""Kalendarz sesji GPW: dni bez sesji wyliczane regułowo na każdy rok.

GPW nie prowadzi sesji w polskie święta ustawowe przypadające w dni robocze oraz dodatkowo
w Wielki Piątek i Sylwestra. Od 2025 r. Wigilia jest dniem wolnym ustawowo. Daty ruchome
(Wielki Piątek, Poniedziałek Wielkanocny, Boże Ciało) liczymy od daty Wielkanocy.
Nietypowe zamknięcia (ogłaszane przez GPW ad hoc) dopisuje się w `schedule.holidays`.
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Iterable

# (miesiąc, dzień) – stałe dni bez sesji
_FIXED = [(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 24), (12, 25), (12, 26), (12, 31)]


def easter(year: int) -> date:
    """Niedziela Wielkanocna (kalendarz gregoriański, algorytm Meeusa/Jonesa/Butchera)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


@lru_cache(maxsize=None)
def gpw_holidays(year: int) -> frozenset[date]:
    """Dni robocze bez sesji na GPW w danym roku (weekendy pomijamy – i tak nie ma sesji)."""
    e = easter(year)
    days = {date(year, m, d) for m, d in _FIXED}
    days |= {e - timedelta(days=2), e + timedelta(days=1), e + timedelta(days=60)}  # WPt, PWlk, Boże Ciało
    return frozenset(d for d in days if d.weekday() < 5)


def is_session_day(d: date, extra_holidays: Iterable[str] = ()) -> bool:
    """Dzień sesyjny: dzień roboczy, nie święto GPW i nie dodatkowe zamknięcie z konfiguracji."""
    return d.weekday() < 5 and d not in gpw_holidays(d.year) and d.isoformat() not in set(extra_holidays)
