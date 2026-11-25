"""Pre-filtr szumu: komunikaty ESPI, których nie warto oceniać agentem (#6).

Ostrożność ważniejsza od oszczędności: reguły patrzą tylko na temat z tytułu ESPI, a słowo z
`_KEEP` w tytule lub treści (dywidenda, skup, wezwanie...) zawsze przepuszcza news do agenta.
"""
from __future__ import annotations

import re

from .config import PrefilterConfig
from .mapping import normalize
from .models import NewsItem, SourceKind

# Tekst po normalize(): wielkie litery bez polskich znaków, twarde spacje zamienione na zwykłe
_RULES: list[tuple[str, re.Pattern]] = [(name, re.compile(rx)) for name, rx in [
    ("zwołanie WZA", r"\b(ZWOLANI\w*|OGLOSZENI\w* O ZWOLANIU)\b.*\b(WALN\w+ ZGROMADZE\w+|N?WZA?|ZWZA?)\b"),
    ("projekty uchwał WZA", r"\bPROJEKT\w* UCHWAL\b"),
    ("lista akcjonariuszy WZA", r"\b(LISTA|WYKAZ) AKCJONARIUSZY\b"),
    ("skład rady nadzorczej", r"\b(ZMIAN\w*|POWOLANI\w*|ODWOLANI\w*|REZYGNACJ\w*|WYBOR\w*)\b.*\bRAD\w+ NADZORCZ\w+"),
    ("terminy raportów", r"\bTERMIN\w*\b.*\bRAPORT\w+"),
    ("wybór audytora", r"\b(WYBOR\w*|ZMIAN\w*)\b.*\b(FIRM\w+ AUDYTORSK\w+|BIEGL\w+ REWIDENT\w*)"),
]]
_KEEP = re.compile(
    r"\b(DYWIDEND|SKUP|BUY-?BACK|WEZWANI|POLACZENI|FUZJ|PRZEJECI|PODZIAL|EMISJ|PODWYZSZENI|UMORZENI|WYCOFANI|"
    r"UMOW|KONTRAKT|PROGNOZ|WYNIK|SPRZEDAZ|NABYCI|ZBYCI|PREZES)")
_INSIDER = re.compile(r"\b(ART\.? ?19\b|BLISKO ZWIAZAN|OBOWIAZKI ZARZADCZE)")
# Kwota liczy się tylko jako wartość transakcji ("łączna wartość: 45 tys. zł"), a nie cena jednej akcji
_VALUE = re.compile(r"\b(?:LACZN\w*|WARTOSC\w*)\b[^0-9]{0,40}(\d[\d .]*(?:,\d+)?) ?(TYS\.?|MLN|MLD)? ?(?:ZL|PLN)\b")
_UNITS = {"": 1.0, "TYS": 1e3, "MLN": 1e6, "MLD": 1e9}


def _amounts(text: str) -> list[float]:
    return [float(re.sub(r"[ .]", "", num).replace(",", ".")) * _UNITS[unit.rstrip(".")]
            for num, unit in _VALUE.findall(text)]


def noise_reason(n: NewsItem, cfg: PrefilterConfig) -> str | None:
    """Powód odrzucenia bez wywołania agenta; None = oceniamy."""
    if not cfg.enabled or n.kind != SourceKind.ESPI:
        return None
    _, sep, subject = n.title.partition(":")
    title = normalize(subject if sep else n.title)
    summary = normalize(n.summary)
    if _INSIDER.search(title):
        # Bez podanej wartości transakcji nie zgadujemy – ocenia agent
        values = _amounts(summary)
        if values and max(values) < cfg.min_insider_value_pln:
            return f"transakcja insidera {max(values):,.0f} zł < {cfg.min_insider_value_pln:,.0f} zł"
        return None
    if _KEEP.search(title) or _KEEP.search(summary):
        return None
    return next((name for name, rx in _RULES if rx.search(title)), None)
