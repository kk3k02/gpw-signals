"""Wskaźniki techniczne potrzebne do poziomów i filtrów."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> float:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])


def sma(series: pd.Series, period: int) -> float:
    if len(series) < period:
        return float("nan")
    return float(series.tail(period).mean())


def swing_points(df: pd.DataFrame, window: int = 5, lookback: int = 120) -> tuple[list[float], list[float]]:
    """Lokalne maksima (opory) i minima (wsparcia): punkt jest ekstremum w oknie ±window."""
    d = df.tail(lookback)
    highs, lows = d["high"].to_numpy(), d["low"].to_numpy()
    res, sup = [], []
    for i in range(window, len(d) - window):
        seg_h, seg_l = highs[i - window:i + window + 1], lows[i - window:i + window + 1]
        if highs[i] == seg_h.max():
            res.append(float(highs[i]))
        if lows[i] == seg_l.min():
            sup.append(float(lows[i]))
    # Ostatnie maksimum z całego okresu też jest istotnym oporem
    res.append(float(d["high"].max()))
    return sorted(set(res)), sorted(set(sup))


@dataclass
class Snapshot:
    """Zestaw liczb opisujących spółkę na dziś – wejście dla filtrów, levels i narzędzi agenta."""
    close: float
    prev_close: float
    atr: float
    sma20: float
    sma50: float
    sma200: float
    avg_turnover_20: float
    volume_ratio: float
    change_pct: float
    high_52w: float
    low_52w: float
    resistances: list[float]
    supports: list[float]
    price_source: str = "daily"   # "daily" (świece dzienne) | "intraday" (bieżący kurs w sesji, #4)

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) and not np.isnan(v) else v)
                for k, v in self.__dict__.items()}


def snapshot(df: pd.DataFrame, atr_period: int = 14, swing_window: int = 5, swing_lookback: int = 120,
             live_price: float | None = None, session_date: date | None = None) -> Snapshot:
    """Bez `live_price` – wszystko z dziennych świec. Z `live_price` (#4): `close` to bieżący kurs,
    a `prev_close`/`change_pct` liczone od wczorajszego zamknięcia. Niepełna dzisiejsza świeca
    (data == session_date) jest wtedy pomijana jako "wczoraj"."""
    if len(df) < max(30, atr_period + 2):
        raise ValueError(f"Za mało notowań ({len(df)}) do policzenia wskaźników")
    close = df["close"]
    vol = df["volume"]
    res, sup = swing_points(df, swing_window, swing_lookback)
    avg_vol = float(vol.tail(21).iloc[:-1].mean()) or 1.0
    last, prev, source = float(close.iloc[-1]), float(close.iloc[-2]), "daily"
    if live_price is not None:
        today_bar = session_date is not None and pd.Timestamp(df.index[-1]).date() == session_date
        prev = float(close.iloc[-2]) if today_bar else float(close.iloc[-1])
        last, source = float(live_price), "intraday"
    return Snapshot(
        close=last,
        prev_close=prev,
        atr=atr(df, atr_period),
        sma20=sma(close, 20), sma50=sma(close, 50), sma200=sma(close, 200),
        avg_turnover_20=float((close * vol).tail(20).mean()),
        volume_ratio=float(vol.iloc[-1]) / avg_vol,
        change_pct=(last / prev - 1) * 100,
        high_52w=float(df["high"].tail(250).max()),
        low_52w=float(df["low"].tail(250).min()),
        resistances=res, supports=sup, price_source=source,
    )
