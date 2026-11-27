"""Symulacja Monte Carlo przed publikacją sygnału.

Ścieżki ceny losujemy z historii spółki (bootstrap dziennych świec): każda wylosowana sesja
daje względny high/low/close wobec poprzedniego zamknięcia, więc zachowuje typową zmienność
i zasięg świecy. Start w środku strefy wejścia. Sesja, w której padają i SL, i cel, liczy się
jako SL (wariant ostrożny). Wynik: odsetek ścieżek, w których cel padł przed SL w horyzoncie.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .models import Levels


@dataclass(frozen=True)
class SimulationResult:
    probability: float   # szansa osiągnięcia celu przed SL (0–1)
    p_stop: float        # szansa trafienia SL
    n_paths: int
    horizon_days: int


def simulate_target_first(df: pd.DataFrame, levels: Levels, horizon_days: int, *,
                          target_index: int = 0, n_paths: int = 10_000, lookback_days: int = 250,
                          seed: int | None = None) -> SimulationResult:
    """`target_index` 0 = T1, 1 = T2, 2 = T3."""
    hist = df.tail(lookback_days + 1)
    prev = hist["close"].shift(1)
    rel = pd.DataFrame({"h": hist["high"] / prev, "l": hist["low"] / prev,
                        "c": hist["close"] / prev}).dropna()
    rel = rel[(rel > 0).all(axis=1) & np.isfinite(rel).all(axis=1)]
    if len(rel) < 20:
        raise ValueError(f"za mało historii do symulacji ({len(rel)} sesji)")
    horizon = max(1, int(horizon_days))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(rel), size=(n_paths, horizon))
    h, lo, c = (rel[k].to_numpy()[idx] for k in ("h", "l", "c"))

    # Zamknięcie dnia poprzedniego dla każdej sesji ścieżki (start: środek strefy wejścia)
    closes = levels.entry_mid * np.cumprod(c, axis=1)
    prev_close = np.concatenate([np.full((n_paths, 1), levels.entry_mid), closes[:, :-1]], axis=1)
    target = levels.targets[target_index]
    hit_t = prev_close * h >= target
    hit_sl = prev_close * lo <= levels.stop_loss

    never = horizon + 1
    first_t = np.where(hit_t.any(axis=1), hit_t.argmax(axis=1), never)
    first_sl = np.where(hit_sl.any(axis=1), hit_sl.argmax(axis=1), never)
    win = first_t < first_sl          # ten sam dzień = SL
    stop = (first_sl <= first_t) & (first_sl < never)
    return SimulationResult(float(win.mean()), float(stop.mean()), n_paths, horizon)
