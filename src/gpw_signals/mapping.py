"""Mapowanie treści newsa na tickery GPW (aliasy + dopasowanie rozmyte)."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz


@dataclass
class Company:
    ticker: str
    name: str
    symbol: str            # nazwa pliku/klucz notowań, np. "pkn" -> data/prices/pkn.csv
    sector: str
    aliases: list[str]
    isin: str = ""         # klucz w archiwum notowań GPW


class TickerMap:
    def __init__(self, companies: list[Company]):
        self.companies = {c.ticker: c for c in companies}

    @classmethod
    def from_csv(cls, path: str | Path) -> TickerMap:
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return cls([Company(r["ticker"].upper(), r["name"], r["symbol"], r.get("sector", ""),
                            [a.strip() for a in r.get("aliases", "").split("|") if a.strip()],
                            r.get("isin", "").strip().upper())
                    for r in rows])

    def by_isin(self) -> dict[str, Company]:
        return {c.isin: c for c in self.companies.values() if c.isin}

    def get(self, ticker: str) -> Company | None:
        return self.companies.get(ticker.upper())

    def find(self, text: str, fuzzy_threshold: int = 92) -> list[str]:
        """Zwraca tickery wspomniane w tekście. Najpierw dokładne słowa, potem fuzzy dla dłuższych aliasów."""
        found: list[str] = []
        low = text.lower()
        for c in self.companies.values():
            names = [c.name, *c.aliases]
            exact = any(re.search(rf"(?<!\w){re.escape(n.lower())}(?!\w)", low) for n in names)
            # Sam ticker tylko wielkimi literami (np. "LPP"), żeby nie łapać zwykłych słów
            exact = exact or re.search(rf"(?<![\w]){re.escape(c.ticker)}(?![\w])", text) is not None
            if not exact:
                exact = any(len(n) >= 6 and fuzz.partial_ratio(n.lower(), low) >= fuzzy_threshold for n in names)
            if exact:
                found.append(c.ticker)
        return found
