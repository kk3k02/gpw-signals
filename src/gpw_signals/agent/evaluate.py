"""Porównanie jakości dostawców na oznaczonych newsach (data/eval/agent_cases.yaml).

Każdy przypadek ma ten sam spokojny, syntetyczny kontekst rynkowy, więc różnice wynikają z oceny newsa.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ..config import AgentConfig
from ..db import Database
from ..ingest.macro import EventCalendar
from ..mapping import TickerMap
from ..models import NewsItem, SourceKind
from .base import AgentError, Assessor
from .factory import make_assessor
from .tools import ToolContext


class CalmPrices:
    """Płaski kurs bez reakcji na newsa."""

    def daily(self, symbol: str, days: int = 250) -> pd.DataFrame:
        close = 50 + np.sin(np.arange(days) / 5)
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
        return pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
                             "volume": np.full(days, 200_000.0)}, index=idx)


@dataclass
class Report:
    total: int
    errors: int            # brak poprawnej oceny
    false_positives: int   # sygnał long tam, gdzie go nie powinno być – najdroższy błąd
    false_negatives: int
    strength_in_range: int
    details: list[str]


def load_cases(path: str | Path) -> list[dict]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def run_cases(cases: list[dict], cfg: AgentConfig, tickers: TickerMap,
              make: Callable[[AgentConfig, ToolContext], Assessor] = make_assessor) -> Report:
    rep = Report(len(cases), 0, 0, 0, 0, [])
    assessor = make(cfg, ToolContext(CalmPrices(), tickers, Database(":memory:"), EventCalendar("__brak__.yaml")))
    for c in cases:
        news = NewsItem("eval", SourceKind(c["kind"]), c["title"], "", datetime.now(UTC), c.get("summary", ""))
        try:
            a = assessor.assess(news, c["ticker"], tickers.get(c["ticker"]).name)
        except AgentError as e:
            rep.errors += 1
            rep.details.append(f"BŁĄD  {c['title']}: {e}")
            continue
        signal = a.actionable and a.direction == "long"
        lo, hi = c["strength"]
        rep.strength_in_range += lo <= a.strength <= hi
        rep.false_positives += signal and not c["signal"]
        rep.false_negatives += c["signal"] and not signal
        mark = "ok   " if signal == c["signal"] else "ŹLE  "
        rep.details.append(f"{mark} {c['title']}: sygnał={signal} siła={a.strength:.2f} (oczekiwane "
                           f"{c['signal']}, {lo}-{hi})")
    return rep
