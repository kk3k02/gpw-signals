"""Kontrola jakości notowań przed liczeniem poziomów.

Archiwum GPW podaje ceny NIESKORYGOWANE o splity, scalenia i prawa poboru. Taki skok w historii
psuje ATR, swingi i SMA, więc spółkę z podejrzanym skokiem w oknie analizy pomijamy, zamiast
liczyć na niej poziomy. Pełna korekta historii to osobny temat (potrzebne dane o zdarzeniach korporacyjnych).
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from .prices import COLUMNS


def data_issues(df: pd.DataFrame, today: date, max_age_days: int = 5, max_jump_pct: float = 40.0,
                lookback: int = 120) -> list[str]:
    """Lista problemów z danymi. Pusta = dane nadają się do liczenia poziomów."""
    if df.empty:
        return ["brak notowań"]
    issues: list[str] = []
    if df.index.has_duplicates:
        issues.append(f"zdublowane daty: {df.index[df.index.duplicated()].strftime('%Y-%m-%d').tolist()[:3]}")
    if df[COLUMNS[:4]].isna().any().any():
        issues.append("brakujące ceny (NaN)")
    if (df[COLUMNS[:4]] <= 0).any().any():
        issues.append("ceny <= 0")
    body_hi, body_lo = df[["open", "close"]].max(axis=1), df[["open", "close"]].min(axis=1)
    bad = (df["high"] < body_hi - 1e-9) | (df["low"] > body_lo + 1e-9)
    if bad.any():
        issues.append(f"niespójne OHLC w {int(bad.sum())} sesjach (np. {df.index[bad][0]:%Y-%m-%d})")

    age = (pd.Timestamp(today) - df.index.max()).days
    if age > max_age_days:
        issues.append(f"ostatnie notowanie sprzed {age} dni ({df.index.max():%Y-%m-%d})")

    close = df["close"].tail(lookback + 1)
    jumps = (close / close.shift(1) - 1).abs() * 100
    big = jumps[jumps > max_jump_pct]
    if len(big):
        d, j = big.index[-1], float(big.iloc[-1])
        issues.append(f"skok kursu {j:.0f}% ({d:%Y-%m-%d}) – możliwy split/scalenie, dane nieskorygowane")
    return issues

