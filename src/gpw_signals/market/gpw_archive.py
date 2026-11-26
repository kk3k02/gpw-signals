"""Import notowań dziennych z oficjalnego archiwum GPW (gpw.pl/archiwum-notowan).

Jeden plik Excel na dzień sesji zawiera OHLCV wszystkich akcji (type=10) albo indeksów (type=1).
Importer zapisuje dane do data/prices/{symbol}.csv, które czyta CsvDirProvider.

Zasady:
- surowe pliki trafiają do cache (data/raw/gpw), więc ponowny import (np. po dopisaniu spółek
  do gpw_tickers.csv) nie pobiera niczego z sieci,
- zapytania są rozłożone w czasie (min_interval_s) i identyfikują klienta w User-Agent,
- dzień bez sesji zwraca stronę HTML zamiast Excela – zapamiętujemy go jako "brak sesji".

UWAGA licencyjna: dane z gpw.pl są danymi opóźnionymi z serwisu GPW. Przed publicznym
udostępnianiem sygnałów sprawdź warunki korzystania (Polityka dotycząca Danych Rynkowych GPW, #16).
"""
from __future__ import annotations

import io
import logging
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .calendar import is_session_day
from .prices import COLUMNS, parse_ohlcv_csv

log = logging.getLogger(__name__)

TYPE_SHARES = 10
TYPE_INDICES = 1
DEFAULT_URL = "https://www.gpw.pl/archiwum-notowan?fetch=1&type={type}&instrument=&date={date}"

# Fragment znormalizowanej nazwy kolumny -> nasza nazwa. Nagłówki GPW mają uszkodzone polskie
# znaki (np. "Kurs zamkni�cia"), więc dopasowujemy po fragmentach bez diakrytyków.
_COLUMN_KEYS = [("data", "date"), ("nazwa", "name"), ("isin", "isin"), ("otwarcia", "open"),
                ("kurs max", "high"), ("kurs min", "low"), ("zamkni", "close"), ("wolumen", "volume")]
_XLS_MAGIC = (b"\xd0\xcf\x11\xe0", b"PK\x03\x04")  # .xls (OLE2) i .xlsx (zip)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def is_excel(content: bytes) -> bool:
    return content[:4] in _XLS_MAGIC


def normalize_archive(raw: pd.DataFrame, require_volume: bool = True) -> pd.DataFrame:
    """Kolumny archiwum GPW -> date, name, isin, open, high, low, close, volume.

    Pomija wiersze bez notowania (kurs otwarcia 0, brak cen). Dla akcji (`require_volume`) także
    wiersze z wolumenem 0 – spółka nie miała tego dnia transakcji. Indeksy mają w archiwum
    zawsze wolumen 0, więc dla nich ten warunek trzeba wyłączyć.
    """
    mapping: dict[str, str] = {}
    for col in raw.columns:
        n = _norm(col)
        for key, target in _COLUMN_KEYS:
            if key in n and target not in mapping.values():
                mapping[col] = target
                break
    missing = {"date", "name", "isin", *COLUMNS} - set(mapping.values())
    if missing:
        raise ValueError(f"Nieznany format archiwum GPW, brak kolumn: {sorted(missing)}; są: {list(raw.columns)}")
    df = raw.rename(columns=mapping)[["date", "name", "isin", *COLUMNS]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["name"] = df["name"].astype(str).str.strip()
    df["isin"] = df["isin"].astype(str).str.strip().str.upper()
    for c in COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    quoted = (df["open"] > 0) & df[["open", "high", "low", "close"]].notna().all(axis=1)
    if require_volume:
        quoted &= df["volume"] > 0
    df["volume"] = df["volume"].fillna(0)
    return df[quoted].reset_index(drop=True)


def parse_archive(content: bytes, require_volume: bool = True) -> pd.DataFrame | None:
    """Plik z archiwum -> DataFrame; None, jeśli to nie Excel (dzień bez sesji / brak danych)."""
    if not is_excel(content):
        return None
    return normalize_archive(pd.read_excel(io.BytesIO(content), engine="calamine"), require_volume)


class GpwArchiveClient:
    """Pobieranie plików dziennych z cache na dysku i grzecznym tempem zapytań."""

    def __init__(self, raw_dir: str | Path, url_template: str = DEFAULT_URL, min_interval_s: float = 1.5,
                 fetch: Callable[[str], bytes] | None = None):
        self.raw_dir = Path(raw_dir)
        self.url_template = url_template
        self.min_interval_s = min_interval_s
        self._fetch = fetch or self._http_get
        self._last = 0.0

    def _paths(self, day: date, type_: int) -> tuple[Path, Path]:
        base = self.raw_dir / str(type_) / day.isoformat()
        return base.with_suffix(".xls"), base.with_suffix(".none")

    @retry(retry=retry_if_exception_type(httpx.TransportError), stop=stop_after_attempt(3),
           wait=wait_exponential(min=2, max=20), reraise=True)
    def _http_get(self, url: str) -> bytes:
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = httpx.get(url, timeout=30, follow_redirects=True,
                          headers={"User-Agent": "gpw-signals/0.1 (daily archive import)"})
        finally:
            self._last = time.monotonic()
        r.raise_for_status()
        return r.content

    def fetch_day(self, day: date, type_: int, today: date) -> bytes | None:
        """Surowy plik dnia albo None (brak sesji). Brak sesji zapamiętujemy tylko dla dni przeszłych –
        dzisiejszy plik może pojawić się później."""
        xls, none = self._paths(day, type_)
        if xls.exists():
            return xls.read_bytes()
        if none.exists():
            return None
        content = self._fetch(self.url_template.format(type=type_, date=day.strftime("%d-%m-%Y")))
        xls.parent.mkdir(parents=True, exist_ok=True)
        if is_excel(content):
            xls.write_bytes(content)
            return content
        if day < today:
            none.touch()
        return None


class PriceStore:
    """Zapis do data/prices/{symbol}.csv (upsert po dacie, rosnąco)."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def path(self, symbol: str) -> Path:
        return self.dir / f"{symbol}.csv"

    def read(self, symbol: str) -> pd.DataFrame:
        p = self.path(symbol)
        if not p.exists():
            return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="date"))
        return parse_ohlcv_csv(p.read_text(encoding="utf-8"))

    def upsert(self, symbol: str, rows: pd.DataFrame) -> None:
        """`rows`: kolumny date + COLUMNS. Nowsze dane dla tej samej daty nadpisują stare."""
        new = rows.set_index("date")[COLUMNS]
        old = self.read(symbol)
        merged = pd.concat([old, new]) if len(old) else new
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        merged.index.name = "date"
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path(symbol).with_suffix(".tmp")
        merged.to_csv(tmp, float_format="%.6g")
        tmp.replace(self.path(symbol))  # podmiana atomowa: czytający nie zobaczy połowy pliku

    def last_date(self, symbol: str) -> date | None:
        df = self.read(symbol)
        return df.index.max().date() if len(df) else None


@dataclass
class ImportSummary:
    sessions: int = 0
    no_session: int = 0
    rows: int = 0
    unmapped_isins: set[str] = field(default_factory=set)
    missing_symbols: dict[str, int] = field(default_factory=dict)  # spółka z listy bez notowań danego dnia


def import_days(days: Iterable[date], client: GpwArchiveClient, store: PriceStore,
                share_symbols: dict[str, str], index_symbols: dict[str, str], today: date) -> ImportSummary:
    """Importuje wskazane dni. `share_symbols`: ISIN -> symbol; `index_symbols`: nazwa indeksu -> symbol."""
    summary = ImportSummary()
    buffers: dict[str, list[pd.DataFrame]] = {}
    for day in days:
        shares_raw = client.fetch_day(day, TYPE_SHARES, today)
        shares = parse_archive(shares_raw) if shares_raw else None
        if shares is None:
            summary.no_session += 1
            continue
        summary.sessions += 1
        mapped = shares[shares["isin"].isin(share_symbols)]
        for isin, g in mapped.groupby("isin"):
            buffers.setdefault(share_symbols[isin], []).append(g)
        for isin, sym in share_symbols.items():
            if isin not in set(mapped["isin"]):
                summary.missing_symbols[sym] = summary.missing_symbols.get(sym, 0) + 1
        summary.unmapped_isins |= set(shares["isin"]) - set(share_symbols)

        idx_raw = client.fetch_day(day, TYPE_INDICES, today)
        indices = parse_archive(idx_raw, require_volume=False) if idx_raw else None
        if indices is not None:
            for name, sym in index_symbols.items():
                g = indices[indices["name"].str.upper() == name.upper()]
                if len(g):
                    buffers.setdefault(sym, []).append(g)

    for sym, parts in buffers.items():
        rows = pd.concat(parts)
        store.upsert(sym, rows)
        summary.rows += len(rows)
    return summary


def session_days(start: date, end: date, holidays: Iterable[str] = ()) -> list[date]:
    """Dni sesyjne z przedziału [start, end]: bez weekendów, świąt GPW i zamknięć z konfiguracji."""
    skip = set(holidays)
    out, d = [], start
    while d <= end:
        if is_session_day(d, skip):
            out.append(d)
        d += timedelta(days=1)
    return out


def sync(client: GpwArchiveClient, store: PriceStore, share_symbols: dict[str, str],
         index_symbols: dict[str, str], today: date, history_days: int, holidays: Iterable[str] = (),
         start: date | None = None, end: date | None = None) -> ImportSummary:
    """Dociąga brakujące dni. Bez `start`: od najstarszej z ostatnich dat w plikach (a dla spółki bez
    pliku – na tyle wstecz, by mieć `history_days` sesji). Pliki z cache nie są pobierane ponownie."""
    if start is None:
        lasts = [store.last_date(s) for s in [*share_symbols.values(), *index_symbols.values()]]
        if not lasts or any(d is None for d in lasts):
            start = today - timedelta(days=int(history_days * 365 / 250) + 14)
        else:
            start = min(lasts) + timedelta(days=1)
    days = session_days(start, end or today, holidays)
    log.info("Import notowań GPW: %s..%s (%d dni roboczych)", start, end or today, len(days))
    summary = import_days(days, client, store, share_symbols, index_symbols, today)
    log.info("Import zakończony: sesji %d, dni bez sesji %d, zapisanych wierszy %d, spółek GPW spoza listy %d",
             summary.sessions, summary.no_session, summary.rows, len(summary.unmapped_isins))
    if summary.missing_symbols:
        # Zwykle małe spółki bez transakcji w części sesji – kontrola jakości oceni, czy dane są aktualne
        worst = sorted(summary.missing_symbols.items(), key=lambda kv: -kv[1])[:5]
        log.info("Spółki bez obrotu w części sesji: %d (najwięcej braków: %s)", len(summary.missing_symbols),
                 ", ".join(f"{k}={v}" for k, v in worst))
    return summary
