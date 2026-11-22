"""Zaokrąglanie cen do kroku notowań.

UWAGA: to UPROSZCZONA tabela zależna tylko od ceny. Na GPW krok notowań zależy także od pasma
płynności instrumentu (reżim tick size MiFID II). Przed produkcją podmień na oficjalną tabelę GPW.
"""
from __future__ import annotations

import math

# (górna granica ceny, krok)
_TABLE: list[tuple[float, float]] = [
    (1.0, 0.001), (5.0, 0.002), (10.0, 0.005), (50.0, 0.01),
    (100.0, 0.02), (500.0, 0.05), (1000.0, 0.1), (math.inf, 0.5),
]


def tick_for(price: float) -> float:
    for upper, tick in _TABLE:
        if price < upper:
            return tick
    return _TABLE[-1][1]


def _decimals(tick: float) -> int:
    return max(0, -math.floor(math.log10(tick))) if tick < 1 else 0


def round_down(price: float) -> float:
    t = tick_for(price)
    return round(math.floor(price / t + 1e-9) * t, _decimals(t))


def round_up(price: float) -> float:
    t = tick_for(price)
    return round(math.ceil(price / t - 1e-9) * t, _decimals(t))
