"""Deterministyczne wyznaczanie strefy wejścia, T1–T3 i StopLoss (long).

Zasada: LLM nigdy nie wymyśla cen. Poziomy wynikają wyłącznie z notowań (ATR, swingi) i parametrów.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import LevelsConfig
from ..market.indicators import Snapshot
from ..market.tick_size import DEFAULT_BAND, round_down, round_up, tick_for
from ..models import Levels


@dataclass
class LevelsResult:
    levels: Levels | None
    reason: str = ""


def compute_levels(snap: Snapshot, cfg: LevelsConfig, band: int = DEFAULT_BAND) -> LevelsResult:
    a = snap.atr
    if a <= 0:
        return LevelsResult(None, "ATR = 0")

    # 1) Strefa wejścia wokół bieżącej ceny
    entry_min = round_down(snap.close - cfg.entry_below_atr * a, band)
    entry_max = round_up(snap.close + cfg.entry_above_atr * a, band)

    # 2) StopLoss: pod ATR albo pod najbliższym wsparciem (swing low), jeśli jest w rozsądnej odległości
    sl = entry_min - cfg.sl_atr * a
    below = [s for s in snap.supports if s < entry_min]
    if below:
        nearest = max(below)
        if entry_min - nearest <= 2 * cfg.sl_atr * a:
            sl = min(sl, nearest - cfg.sl_swing_buffer_atr * a)
    sl = round_down(sl, band)
    if sl <= 0:
        return LevelsResult(None, "SL <= 0")

    mid = (entry_min + entry_max) / 2
    sl_pct = (1 - sl / mid) * 100
    if sl_pct > cfg.max_sl_pct:
        return LevelsResult(None, f"SL za daleko ({sl_pct:.1f}% > {cfg.max_sl_pct}%)")

    # 3) Cele jako wielokrotności R, "przyciągane" tuż pod najbliższy opór
    r = mid - sl
    targets: list[float] = []
    prev = entry_max
    for m in cfg.r_multiples:
        raw = mid + m * r
        blockers = [x for x in snap.resistances if prev < x < raw and raw - x < cfg.resistance_snap_r * r]
        t = (max(blockers) - tick_for(max(blockers), band)) if blockers else raw
        t = round_down(t, band)
        if t <= prev:  # opór zbyt blisko poprzedniego celu – zostaw cel "surowy"
            t = round_down(raw, band)
        if t <= prev:
            return LevelsResult(None, "Nie da się ułożyć rosnących celów")
        targets.append(t)
        prev = t

    return LevelsResult(Levels(entry_min, entry_max, targets, sl))
