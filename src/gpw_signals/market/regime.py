"""Reżim rynku na podstawie indeksu (domyślnie WIG20)."""
from __future__ import annotations

from enum import StrEnum

import pandas as pd

from .indicators import sma


class Regime(StrEnum):
    BULL = "bull"
    NEUTRAL = "neutral"
    BEAR = "bear"


def market_regime(index_df: pd.DataFrame) -> Regime:
    close = index_df["close"]
    last, s50, s200 = float(close.iloc[-1]), sma(close, 50), sma(close, 200)
    if pd.isna(s200):
        s200 = s50
    if last > s50 > s200:
        return Regime.BULL
    if last < s50 < s200:
        return Regime.BEAR
    return Regime.NEUTRAL
