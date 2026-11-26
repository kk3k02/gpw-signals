"""Krok notowań na GPW: reżim tick size MiFID II (RTS 11, rozporządzenie delegowane (UE) 2017/588).

Krok zależy od ceny i pasma płynności instrumentu. Pasmo (1–6) wynika ze średniej dziennej liczby
transakcji (ADNT) na najważniejszym rynku; nadzór publikuje ją w rejestrze ESMA FITRS, a pasma
obowiązują od 1 kwietnia. Przypisanie do spółek: kolumna `liquidity_band` w data/gpw_tickers.csv
(polecenie `update-ticks`).
"""
from __future__ import annotations

import math
from bisect import bisect_right

# Dolne granice ADNT kolejnych pasm
_ADNT_BOUNDS = (0, 10, 80, 600, 2000, 9000)
BANDS = range(1, len(_ADNT_BOUNDS) + 1)
# Spółka bez pasma w pliku (np. świeżo dopisana przez build-tickers, przed update-ticks)
DEFAULT_BAND = 2

# Załącznik RTS 11: (dolna granica ceny, kroki dla pasm 1..6)
_TABLE: list[tuple[float, tuple[float, ...]]] = [
    (0, (0.0005, 0.0002, 0.0001, 0.0001, 0.0001, 0.0001)),
    (0.1, (0.001, 0.0005, 0.0002, 0.0001, 0.0001, 0.0001)),
    (0.2, (0.002, 0.001, 0.0005, 0.0002, 0.0001, 0.0001)),
    (0.5, (0.005, 0.002, 0.001, 0.0005, 0.0002, 0.0001)),
    (1, (0.01, 0.005, 0.002, 0.001, 0.0005, 0.0002)),
    (2, (0.02, 0.01, 0.005, 0.002, 0.001, 0.0005)),
    (5, (0.05, 0.02, 0.01, 0.005, 0.002, 0.001)),
    (10, (0.1, 0.05, 0.02, 0.01, 0.005, 0.002)),
    (20, (0.2, 0.1, 0.05, 0.02, 0.01, 0.005)),
    (50, (0.5, 0.2, 0.1, 0.05, 0.02, 0.01)),
    (100, (1, 0.5, 0.2, 0.1, 0.05, 0.02)),
    (200, (2, 1, 0.5, 0.2, 0.1, 0.05)),
    (500, (5, 2, 1, 0.5, 0.2, 0.1)),
    (1000, (10, 5, 2, 1, 0.5, 0.2)),
    (2000, (20, 10, 5, 2, 1, 0.5)),
    (5000, (50, 20, 10, 5, 2, 1)),
    (10000, (100, 50, 20, 10, 5, 2)),
    (20000, (200, 100, 50, 20, 10, 5)),
    (50000, (500, 200, 100, 50, 20, 10)),
]
_PRICE_BOUNDS = [lower for lower, _ in _TABLE]


def band_for_adnt(adnt: float) -> int:
    if adnt < 0:
        raise ValueError(f"ADNT nie może być ujemne: {adnt}")
    return bisect_right(_ADNT_BOUNDS, adnt)


def tick_for(price: float, band: int) -> float:
    if band not in BANDS:
        raise ValueError(f"Nieznane pasmo płynności: {band}")
    row = max(0, bisect_right(_PRICE_BOUNDS, price) - 1)
    return _TABLE[row][1][band - 1]


def _decimals(tick: float) -> int:
    return max(0, -math.floor(math.log10(tick))) if tick < 1 else 0


def round_down(price: float, band: int) -> float:
    t = tick_for(price, band)
    return round(math.floor(price / t + 1e-9) * t, _decimals(t))


def round_up(price: float, band: int) -> float:
    t = tick_for(price, band)
    return round(math.ceil(price / t - 1e-9) * t, _decimals(t))
