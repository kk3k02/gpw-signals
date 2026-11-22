"""Notowania dzienne OHLCV. Domyślnie stooq (CSV, dane opóźnione) – podmień na płatny feed w produkcji."""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Protocol

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

COLUMNS = ["open", "high", "low", "close", "volume"]


class PriceProvider(Protocol):
    def daily(self, symbol: str, days: int = 250) -> pd.DataFrame:
        """DataFrame z indeksem dat i kolumnami open/high/low/close/volume (rosnąco po dacie)."""
        ...


class StooqProvider:
    _RENAME = {"Data": "date", "Otwarcie": "open", "Najwyzszy": "high", "Najnizszy": "low",
               "Zamkniecie": "close", "Wolumen": "volume",
               "Date": "date", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}

    def __init__(self, url_template: str, cache_ttl_s: int = 600):
        self.url_template = url_template
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, pd.DataFrame]] = {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def _fetch(self, symbol: str) -> str:
        r = httpx.get(self.url_template.format(symbol=symbol), timeout=20,
                      headers={"User-Agent": "gpw-signals/0.1"})
        r.raise_for_status()
        return r.text

    def daily(self, symbol: str, days: int = 250) -> pd.DataFrame:
        hit = self._cache.get(symbol)
        if hit and time.time() - hit[0] < self.cache_ttl_s:
            return hit[1].tail(days)
        df = parse_ohlcv_csv(self._fetch(symbol), self._RENAME)
        self._cache[symbol] = (time.time(), df)
        return df.tail(days)


class CsvDirProvider:
    """Notowania z lokalnych plików {dir}/{symbol}.csv (kolumny date,open,high,low,close,volume).

    Punkt wpięcia dla własnego/licencjonowanego źródła danych: osobny proces zapisuje CSV, bot je czyta.
    """

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def daily(self, symbol: str, days: int = 250) -> pd.DataFrame:
        p = self.dir / f"{symbol}.csv"
        if not p.exists():
            raise FileNotFoundError(f"Brak pliku notowań {p}")
        return parse_ohlcv_csv(p.read_text(encoding="utf-8"), StooqProvider._RENAME).tail(days)


def make_provider(provider: str, stooq_url: str, csv_dir: str) -> PriceProvider:
    if provider == "csv":
        return CsvDirProvider(csv_dir)
    if provider == "stooq":
        return StooqProvider(stooq_url)
    raise ValueError(f"Nieznany provider notowań: {provider}")


def parse_ohlcv_csv(text: str, rename: dict[str, str] | None = None) -> pd.DataFrame:
    if text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        raise ValueError("Źródło notowań zwróciło stronę HTML zamiast CSV (blokada automatów lub wymagany "
                         "klucz API). Użyj licencjonowanego źródła albo market.provider: csv.")
    df = pd.read_csv(io.StringIO(text))
    if rename:
        df = df.rename(columns=rename)
    missing = {"date", *COLUMNS[:4]} - set(df.columns)
    if missing:
        raise ValueError(f"Niepoprawny CSV notowań, brak kolumn: {missing}. Początek: {text[:120]!r}")
    if "volume" not in df.columns:
        df["volume"] = 0
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()[COLUMNS].astype(float)
