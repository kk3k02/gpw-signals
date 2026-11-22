"""Budowa pełnej listy spółek (data/gpw_tickers.csv) z plików archiwum GPW.

Źródło: ISIN i nazwa skrócona z ostatnich sesji (cache data/raw/gpw/10). Wiersze już obecne
w pliku (ręcznie opisane: ticker, nazwa, aliasy, sektor) zostają bez zmian – generator tylko
dopisuje brakujące spółki i uzupełnia `short_name`. Dla nowych spółek tickerem jest nazwa
skrócona GPW (np. POLIMEXMS); trzyliterowy skrót i aliasy można dopisać ręcznie później.
"""
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .mapping import CSV_FIELDS

log = logging.getLogger(__name__)


@dataclass
class UniverseUpdate:
    rows: list[dict]
    added: list[str] = field(default_factory=list)
    filled_short_names: list[str] = field(default_factory=list)
    not_in_archive: list[str] = field(default_factory=list)  # z pliku, brak w ostatnich sesjach (np. wycofane)
    skipped_conflicts: list[str] = field(default_factory=list)


def _symbol(short_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", short_name.lower())


def read_rows(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8", newline="") as f:
        return [{k: (r.get(k) or "").strip() for k in CSV_FIELDS} for r in csv.DictReader(f)]


def write_rows(path: str | Path, rows: list[dict]) -> None:
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(p)


def update_universe(existing: list[dict], archive: pd.DataFrame) -> UniverseUpdate:
    """`archive`: kolumny isin, name (nazwa skrócona) z ostatnich sesji archiwum GPW."""
    latest = archive.drop_duplicates("isin", keep="last").set_index("isin")["name"].to_dict()
    rows = [dict(r) for r in existing]
    upd = UniverseUpdate(rows)
    by_isin = {r["isin"].upper(): r for r in rows if r["isin"]}
    tickers = {r["ticker"].upper() for r in rows}
    symbols = {r["symbol"] for r in rows}

    for r in rows:
        isin = r["isin"].upper()
        if isin in latest and not r["short_name"]:
            r["short_name"] = latest[isin]
            upd.filled_short_names.append(r["ticker"])
        elif isin and isin not in latest:
            upd.not_in_archive.append(r["ticker"])

    for isin, short in sorted(latest.items(), key=lambda kv: kv[1]):
        if isin in by_isin:
            continue
        ticker, symbol = short.upper(), _symbol(short)
        if ticker in tickers or symbol in symbols or not symbol:
            upd.skipped_conflicts.append(f"{short} ({isin})")
            continue
        rows.append({"ticker": ticker, "name": short, "symbol": symbol, "isin": isin, "short_name": short,
                     "sector": "", "aliases": ""})
        tickers.add(ticker)
        symbols.add(symbol)
        upd.added.append(ticker)
    return upd
