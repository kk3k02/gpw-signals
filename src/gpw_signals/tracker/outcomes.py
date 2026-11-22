"""Śledzenie wyników opublikowanych sygnałów: T1/T2/T3/SL/wygaśnięcie."""
from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from ..clock import MARKET_TZ, market_date
from ..models import Signal, SignalStatus

_ORDER = [SignalStatus.ACTIVE, SignalStatus.T1, SignalStatus.T2, SignalStatus.T3]


@dataclass
class Outcome:
    status: SignalStatus   # najwyższy osiągnięty poziom (albo STOPPED / EXPIRED)
    closed: bool


def evaluate(signal: Signal, bars: pd.DataFrame, tz: ZoneInfo = MARKET_TZ) -> Outcome:
    """Ocena na podstawie sesji PO dniu publikacji (przelicza całą historię – idempotentnie).

    - Sygnał „startuje”, gdy kurs wejdzie w strefę (low <= entry_max).
    - Jeśli w jednej sesji padł i SL, i cel – liczymy SL (nie znamy kolejności intraday).
    - SL po osiągnięciu T1 zamyka sygnał, ale status zostaje T1/T2 (zysk częściowy).
    - Brak T1 w horyzoncie => EXPIRED; T1/T2 bez T3 w horyzoncie => zamknięty z tym statusem.
    """
    lv = signal.levels
    status = SignalStatus.ACTIVE
    # Świece dzienne są w datach sesji (czas PL) – porównujemy z datą sesji publikacji, nie z datą UTC
    after = bars[bars.index.normalize() > pd.Timestamp(market_date(signal.created_at, tz))]
    entered = False
    for _, b in after.iterrows():
        if not entered:
            entered = b["low"] <= lv.entry_max
            if not entered:
                continue
        if b["low"] <= lv.stop_loss:
            return Outcome(SignalStatus.STOPPED if status == SignalStatus.ACTIVE else status, True)
        for i, t in enumerate(lv.targets, start=1):
            if b["high"] >= t and _ORDER.index(status) < i:
                status = _ORDER[i]
        if status == SignalStatus.T3:
            return Outcome(status, True)

    if len(after) >= signal.assessment.horizon_days:
        return Outcome(SignalStatus.EXPIRED if status == SignalStatus.ACTIVE else status, True)
    return Outcome(status, False)
