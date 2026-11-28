"""Mapowanie newsów na spółki GPW.

Dwie ścieżki o różnej pewności:
- komunikat ESPI/EBI: emitent jest w tytule ("EMITENT: temat") – dopasowujemy TYLKO jego,
  bo treść komunikatu wymienia kontrahentów i akcjonariuszy, którzy nie są emitentem,
- pozostałe newsy: dokładne dopasowanie nazw i aliasów jako całych słów (bez dopasowania
  rozmytego – przy pełnej liście spółek dawało fałszywe trafienia, np. "Narodowy Bank Polski").

Nazwa skrócona GPW (`short_name`, np. PKNORLEN) i ticker są dopasowywane w treści tylko jako
samodzielne słowo pisane WIELKIMI literami – inaczej "POLICE" łapałoby miasto Police.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .market.tick_size import BANDS, DEFAULT_BAND
from .models import SourceKind

CSV_FIELDS = ["ticker", "name", "symbol", "isin", "short_name", "sector", "aliases", "liquidity_band"]

# Formy prawne usuwane z nazwy emitenta przed porównaniem
_LEGAL = re.compile(
    r"\b(SPOLKA AKCYJNA|SPOLKA KOMANDYTOWO-AKCYJNA|S\.?A\.?|S\.?K\.?A\.?|SE|N\.?V\.?|PLC|AG|ASI|LTD|LIMITED)\b\.?")
_MIN_ABBREV = 4
# Skróty, które w newsach oznaczają zwykle coś innego niż spółkę (GPW = giełda jako rynek).
# Te spółki dopasowujemy w treści tylko po pełnej nazwie/aliasach.
_STRICT_STOP = {"GPW"}


def normalize(text: str) -> str:
    """Wielkie litery bez polskich znaków: 'Spółka Akcyjna' -> 'SPOLKA AKCYJNA'."""
    text = unicodedata.normalize("NFKD", text).replace("ł", "l").replace("Ł", "L")
    return "".join(c for c in text if not unicodedata.combining(c)).upper()


def issuer_key(name: str) -> str:
    """Klucz porównania nazw emitentów: bez formy prawnej, tylko litery i cyfry."""
    n = _LEGAL.sub(" ", normalize(name))
    return re.sub(r"[^A-Z0-9]", "", n)


def _is_subsequence(short: str, full: str) -> bool:
    it = iter(full)
    return all(ch in it for ch in short)


def abbreviates(short: str, key: str) -> bool:
    """Czy nazwa skrócona GPW jest skrótem pełnej nazwy (POLIMEXMS ~ POLIMEXMOSTOSTAL, AIRWAY ~ AIRWAYMEDIX).

    Wymagamy wspólnego początku (min. 4 znaki), żeby podciąg liter nie łączył przypadkowych nazw.
    """
    if len(short) < _MIN_ABBREV or len(key) < len(short):
        return False
    if key.startswith(short):
        return True
    return key[:_MIN_ABBREV] == short[:_MIN_ABBREV] and _is_subsequence(short, key)


def espi_issuer(title: str) -> str | None:
    """Emitent z tytułu komunikatu w formacie 'EMITENT: temat'."""
    head, sep, _ = title.partition(":")
    return head.strip() if sep and head.strip() else None


@dataclass
class Company:
    ticker: str
    name: str
    symbol: str            # nazwa pliku/klucz notowań, np. "pkn" -> data/prices/pkn.csv
    sector: str
    aliases: list[str]
    isin: str = ""         # klucz w archiwum notowań GPW
    short_name: str = ""   # nazwa skrócona z archiwum GPW, np. PKNORLEN
    liquidity_band: int = DEFAULT_BAND  # pasmo płynności RTS 11 (krok notowań)
    _patterns: list[re.Pattern] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.short_name = self.short_name.upper()
        if self.liquidity_band not in BANDS:
            raise ValueError(f"{self.ticker}: nieznane pasmo płynności {self.liquidity_band}")
        # Nazwy dopasowywane bez względu na wielkość liter: aliasy i pełna nazwa, o ile nie jest
        # tylko nazwą skróconą/tickerem (te dopasowujemy wyłącznie WIELKIMI literami)
        loose = [*self.aliases]
        # (porównanie dosłowne: "mBank" to pełna nazwa, a "MBANK" w wierszu generowanym – tylko skrót)
        if self.name not in {self.short_name, self.ticker}:
            loose.append(self.name)
        self._patterns = [re.compile(rf"(?<![A-Z0-9]){re.escape(normalize(n))}(?![A-Z0-9])")
                          for n in loose if len(n) >= 3]
        strict = {t for t in (self.ticker, self.short_name) if len(t) >= 3 and t not in _STRICT_STOP}
        self._strict = [re.compile(rf"(?<![\w]){re.escape(t)}(?![\w])") for t in strict]

    def issuer_keys(self) -> set[str]:
        return {k for k in (issuer_key(self.name), self.short_name, self.ticker,
                            *(issuer_key(a) for a in self.aliases)) if k}

    def mentioned_in(self, text: str, text_norm: str) -> bool:
        return any(p.search(text_norm) for p in self._patterns) or any(p.search(text) for p in self._strict)


class TickerMap:
    def __init__(self, companies: list[Company]):
        self.companies = {c.ticker: c for c in companies}
        self._by_key: dict[str, set[str]] = {}
        for c in companies:
            for k in c.issuer_keys():
                self._by_key.setdefault(k, set()).add(c.ticker)

    @classmethod
    def from_csv(cls, path: str | Path) -> TickerMap:
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return cls([Company(r["ticker"].upper(), r["name"], r["symbol"], r.get("sector", ""),
                            [a.strip() for a in r.get("aliases", "").split("|") if a.strip()],
                            r.get("isin", "").strip().upper(), r.get("short_name", "").strip(),
                            int(r.get("liquidity_band") or DEFAULT_BAND))
                    for r in rows])

    def by_isin(self) -> dict[str, Company]:
        return {c.isin: c for c in self.companies.values() if c.isin}

    def get(self, ticker: str) -> Company | None:
        return self.companies.get(ticker.upper())

    def find_issuer(self, issuer: str) -> str | None:
        """Ticker emitenta po nazwie z komunikatu. None, gdy brak albo niejednoznaczne."""
        key = issuer_key(issuer)
        if not key:
            return None
        exact = self._by_key.get(key, set())
        if len(exact) == 1:
            return next(iter(exact))
        if exact:
            return None
        hits = {c.ticker for c in self.companies.values() if c.short_name and abbreviates(c.short_name, key)}
        if len(hits) > 1:  # kilka skrótów pasuje – wybieramy najdłuższy, jeśli jest jednoznaczny
            longest = max(len(self.companies[t].short_name) for t in hits)
            hits = {t for t in hits if len(self.companies[t].short_name) == longest}
        return next(iter(hits)) if len(hits) == 1 else None

    def find(self, text: str) -> list[str]:
        """Tickery spółek wspomnianych w tekście (całe słowa, bez dopasowania rozmytego)."""
        norm = normalize(text)
        return [c.ticker for c in self.companies.values() if c.mentioned_in(text, norm)]

    def map_news(self, title: str, summary: str, kind: SourceKind, isin: str = "") -> list[str]:
        """Tickery dla newsa.

        - ISIN podany przez źródło (money.pl): spółka o tym ISIN-ie,
        - ESPI: wyłącznie emitent z tytułu,
        - pozostałe: tylko spółki z TYTUŁU. Lead często wymienia spółki poboczne ("do ataku odniosły
          się m.in. BNP Paribas i Bank Pekao"), a każde takie trafienie to zbędne wywołanie LLM albo
          sygnał na spółce, której news nie dotyczy. Lead i tak trafia do agenta jako kontekst.
        """
        if isin and (c := self.by_isin().get(isin)):
            return [c.ticker]
        if kind == SourceKind.ESPI:
            issuer = espi_issuer(title)
            if issuer is not None:
                t = self.find_issuer(issuer)
                return [t] if t else []
        return self.find(title)
