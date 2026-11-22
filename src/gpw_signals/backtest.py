"""Backtest samego silnika poziomów (bez newsów): jak często z typowej strefy wejścia padają T1–T3 vs SL.

To kalibracja parametrów `levels` – nie mierzy jakości oceny newsów (do tego służy tracker na żywych sygnałach).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

import pandas as pd

from .config import LevelsConfig
from .levels.engine import compute_levels
from .levels.validate import validate_levels
from .market.indicators import snapshot
from .models import Assessment, Signal
from .tracker.outcomes import evaluate


def backtest_levels(df: pd.DataFrame, cfg: LevelsConfig, horizon: int = 10, step: int = 5,
                    warmup: int = 60) -> Counter:
    stats: Counter = Counter()
    dummy = Assessment("X", True, "long", "inne", 0.5, horizon, False, "")
    for i in range(warmup, len(df) - 1, step):
        hist, future = df.iloc[: i + 1], df.iloc[i + 1: i + 1 + horizon]
        lr = compute_levels(snapshot(hist, cfg.atr_period, cfg.swing_window, cfg.swing_lookback), cfg)
        if lr.levels is None or validate_levels(lr.levels):
            stats["skipped"] += 1
            continue
        day = hist.index[-1].to_pydatetime()
        sig = Signal("X", "X", lr.levels, 0.0, dummy, "", datetime(day.year, day.month, day.day))
        out = evaluate(sig, pd.concat([hist.tail(1), future]))
        stats[out.status.value] += 1
    return stats
