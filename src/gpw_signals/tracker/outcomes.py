"""Śledzenie wyników opublikowanych sygnałów: T1/T2/T3/SL/wygaśnięcie.

Wynik (R i %) liczymy przy umownym modelu pozycji: wejście po środku strefy, po 1/3 pozycji
zamykane na T1, T2 i T3, reszta na SL (albo break-even) lub po zamknięciu ostatniej sesji horyzontu.
Bez prowizji. Luka poniżej SL = wyjście po otwarciu, a nie po cenie SL.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo

import pandas as pd

from ..clock import MARKET_TZ, market_date
from ..models import Levels, Signal, SignalStatus

_ORDER = [SignalStatus.ACTIVE, SignalStatus.T1, SignalStatus.T2, SignalStatus.T3]


class CloseReason(StrEnum):
    TARGETS = "targets"                      # padł ostatni cel
    STOP = "stop"                            # SL bez żadnego celu
    STOP_AFTER_TARGET = "stop_after_target"  # pierwotny SL po T1/T2
    BREAKEVEN = "breakeven"                  # SL przesunięty na wejście po T1
    HORIZON = "horizon"                      # koniec horyzontu z otwartą pozycją
    NOT_ENTERED = "not_entered"              # kurs nie wszedł w strefę


@dataclass
class Outcome:
    status: SignalStatus   # najwyższy osiągnięty poziom (albo STOPPED / EXPIRED)
    closed: bool
    reason: CloseReason | None = None   # tylko dla zamkniętych
    result_r: float | None = None       # wynik w R; None = otwarty, bez wejścia albo brak ceny wyjścia
    result_pct: float | None = None


def _close(status: SignalStatus, reason: CloseReason, lv: Levels, hits: int, exit_price: float | None) -> Outcome:
    """Wynik zamkniętej pozycji: `hits` równych części na celach, reszta po `exit_price`."""
    n = len(lv.targets)
    if exit_price is None and hits < n:
        return Outcome(status, True, reason)
    rest = (n - hits) * (exit_price - lv.entry_mid) if hits < n else 0.0
    gain = (sum(t - lv.entry_mid for t in lv.targets[:hits]) + rest) / n
    return Outcome(status, True, reason, round(gain / lv.risk, 3) if lv.risk > 0 else None,
                   round(gain / lv.entry_mid * 100, 2))


def _price(bar: pd.Series, column: str) -> float | None:
    value = bar.get(column)
    return None if value is None or pd.isna(value) else float(value)


def evaluate(signal: Signal, bars: pd.DataFrame, tz: ZoneInfo = MARKET_TZ,
             breakeven_after_t1: bool = False) -> Outcome:
    """Ocena na podstawie sesji PO dniu publikacji (przelicza całą historię – idempotentnie).

    - Sygnał „startuje”, gdy kurs wejdzie w strefę (low <= entry_max).
    - Jeśli w jednej sesji padł i SL, i cel – liczymy SL (nie znamy kolejności intraday).
    - SL po osiągnięciu T1 zamyka sygnał, ale status zostaje T1/T2 (zysk częściowy).
    - `breakeven_after_t1`: od sesji PO tej z T1 stop jest na środku strefy wejścia.
    - Sesje po horyzoncie nie zmieniają wyniku, nawet gdy tracker uruchomiono z opóźnieniem.
    - Brak T1 w horyzoncie => EXPIRED; T1/T2 bez T3 w horyzoncie => zamknięty z tym statusem.
    """
    lv = signal.levels
    status = SignalStatus.ACTIVE
    # Świece dzienne są w datach sesji (czas PL) – porównujemy z datą sesji publikacji, nie z datą UTC
    after = bars[bars.index.normalize() > pd.Timestamp(market_date(signal.created_at, tz))]
    horizon = signal.assessment.horizon_days
    after = after.head(max(horizon, 0))
    entered = False
    stop = lv.stop_loss
    for _, b in after.iterrows():
        if not entered:
            entered = b["low"] <= lv.entry_max
            if not entered:
                continue
        hits = _ORDER.index(status)
        if b["low"] <= stop:
            open_ = _price(b, "open")
            # Luka poniżej stopa – realne wyjście po otwarciu
            exit_price = stop if open_ is None else min(stop, open_)
            if hits == 0:
                return _close(SignalStatus.STOPPED, CloseReason.STOP, lv, 0, exit_price)
            reason = CloseReason.BREAKEVEN if stop > lv.stop_loss else CloseReason.STOP_AFTER_TARGET
            return _close(status, reason, lv, hits, exit_price)
        for i, t in enumerate(lv.targets, start=1):
            if b["high"] >= t and _ORDER.index(status) < i:
                status = _ORDER[i]
        if status == SignalStatus.T3:
            return _close(status, CloseReason.TARGETS, lv, len(lv.targets), None)
        if breakeven_after_t1 and status != SignalStatus.ACTIVE:
            stop = lv.entry_mid

    if len(after) >= horizon:
        if not entered:
            return Outcome(SignalStatus.EXPIRED, True, CloseReason.NOT_ENTERED)
        final = SignalStatus.EXPIRED if status == SignalStatus.ACTIVE else status
        return _close(final, CloseReason.HORIZON, lv, _ORDER.index(status), _price(after.iloc[-1], "close"))
    return Outcome(status, False)
