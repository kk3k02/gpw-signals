"""Bieżący kurs w trakcie sesji (#4) i pora publikacji newsa względem sesji.

Źródło intraday jest za interfejsem `IntradayProvider`. Gdy go brak, kurs jest nieświeży albo
jesteśmy poza sesją, pipeline wraca do dziennych świec (zachowanie sprzed #4).
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from ..clock import MARKET_TZ, to_utc


@dataclass(frozen=True)
class Quote:
    price: float
    at: datetime  # aware


class IntradayProvider(Protocol):
    def last_quote(self, symbol: str) -> Quote | None:
        """Ostatni kurs (np. zamknięcie ostatniej świecy 5/15 min) albo None, gdy brak danych."""
        ...


class NoIntraday:
    """Brak źródła intraday – zawsze fallback do świec dziennych."""

    def last_quote(self, symbol: str) -> Quote | None:
        return None


class CsvIntradayProvider:
    """Kurs z {dir}/{symbol}.csv: kolumny datetime oraz price lub close (np. świece 5 min).

    Osobny proces (licencjonowany feed) dopisuje wiersze, bot czyta ostatni. Daty bez strefy = czas rynku.
    """

    def __init__(self, directory: str | Path, tz: ZoneInfo = MARKET_TZ):
        self.dir = Path(directory)
        self.tz = tz

    def last_quote(self, symbol: str) -> Quote | None:
        p = self.dir / f"{symbol}.csv"
        if not p.exists():
            return None
        df = pd.read_csv(io.StringIO(p.read_text(encoding="utf-8")))
        col = "price" if "price" in df.columns else "close" if "close" in df.columns else None
        if col is None or "datetime" not in df.columns or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        row = df.sort_values("datetime").iloc[-1]
        price = float(row[col])
        if not price > 0:
            return None
        return Quote(price, to_utc(row["datetime"].to_pydatetime(), self.tz))


def make_intraday(provider: str, csv_dir: str) -> IntradayProvider:
    if provider in ("", "none"):
        return NoIntraday()
    if provider == "csv":
        return CsvIntradayProvider(csv_dir)
    raise ValueError(f"Nieznany provider intraday: {provider}")


class SessionPhase(StrEnum):
    PRE = "przed sesją"      # m.in. ESPI 7:00–9:00, przed otwarciem
    IN = "w trakcie sesji"
    AFTER = "po sesji"


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def session_phase(dt: datetime, tz: ZoneInfo, session_open: str = "09:00",
                  session_close: str = "17:05") -> SessionPhase:
    t = to_utc(dt).astimezone(tz).time()
    if t < _hm(session_open):
        return SessionPhase.PRE
    if t < _hm(session_close):
        return SessionPhase.IN
    return SessionPhase.AFTER


def fresh_quote(provider: IntradayProvider, symbol: str, now: datetime, tz: ZoneInfo,
                max_age_min: float, session_open: str = "09:00") -> Quote | None:
    """Kurs z bieżącej sesji, nie starszy niż max_age_min. Inaczej None (fallback do dziennych)."""
    q = provider.last_quote(symbol)
    if q is None:
        return None
    local_now, local_q = to_utc(now).astimezone(tz), to_utc(q.at).astimezone(tz)
    if local_q.date() != local_now.date() or local_q.time() < _hm(session_open):
        return None
    if local_now - local_q > timedelta(minutes=max_age_min):
        return None
    return q
