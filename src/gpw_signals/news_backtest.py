"""Backtest całego pipeline'u na historycznych newsach (#12).

Każdy news przechodzi tę samą ścieżkę co na żywo (`App.process`): mapping -> prefiltr -> cooldown ->
jakość danych -> poziomy -> filtry przed agentem -> agent -> scoring. Zegar aplikacji jest ustawiany
na chwilę publikacji newsa, a notowania są przycinane do tej chwili (`AsOfPrices`) – agent, reżim
rynku i silnik poziomów nie widzą przyszłości. Dopiero wynik sygnału liczy tracker (`evaluate`)
na pełnej historii, czyli na sesjach PO dniu newsa.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd
import yaml

from .agent.base import Assessor
from .clock import MARKET_TZ, market_date, to_utc
from .config import Settings
from .db import Database
from .ingest.macro import EventCalendar
from .mapping import TickerMap
from .market.prices import PriceProvider
from .models import NewsItem, Signal, SourceKind
from .pipeline import App, TransientError
from .publish.telegram import Publisher
from .tracker.outcomes import Outcome, evaluate
from .tracker.stats import summarize

# Po zamknięciu sesji (fixing 17:00 + dogrywka) świeca dnia jest już znana
SESSION_CLOSE = time(17, 5)


class AsOfPrices:
    """Notowania widziane „z chwili newsa”: tylko sesje zakończone przed `as_of`.

    News w trakcie sesji (lub przed nią) widzi świece do poprzedniej sesji – świeca dnia newsa
    zawiera ruch PO newsie i byłaby wyciekiem przyszłości. News po zamknięciu widzi też sesję z tego dnia.
    """

    def __init__(self, inner: PriceProvider, history_days: int = 5000):
        self.inner, self.history_days = inner, history_days
        self.as_of: datetime | None = None
        self._cache: dict[str, pd.DataFrame] = {}

    def full(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._cache:
            self._cache[symbol] = self.inner.daily(symbol, self.history_days)
        return self._cache[symbol]

    def last_visible_session(self) -> date:
        local = to_utc(self.as_of).astimezone(MARKET_TZ)
        return local.date() if local.time() >= SESSION_CLOSE else date.fromordinal(local.date().toordinal() - 1)

    def daily(self, symbol: str, days: int = 250) -> pd.DataFrame:
        df = self.full(symbol)
        if self.as_of is not None:
            df = df[df.index.normalize() <= pd.Timestamp(self.last_visible_session())]
        return df.tail(days)


class _SilentPublisher(Publisher):
    def send(self, text: str, reply_to: int | None = None) -> int | None:
        return None


# ---------------------------------------------------------------- zbiór newsów

def load_news(path: str | Path) -> list[NewsItem]:
    """YAML: lista {published_at, title, kind, source?, summary?, url?, priority?}.
    `published_at` bez strefy = czas warszawski (tak podaje ESPI)."""
    items = []
    for r in yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []:
        ts = r["published_at"]
        ts = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
        items.append(NewsItem(r.get("source", r["kind"]), SourceKind(r["kind"]), r["title"], r.get("url", ""),
                              to_utc(ts, MARKET_TZ), r.get("summary", ""), int(r.get("priority", 1))))
    return items


# ---------------------------------------------------------------- przebieg

@dataclass
class BacktestSignal:
    signal: Signal
    source: str
    kind: str
    outcome: Outcome

    def row(self) -> dict:
        """Wiersz w kształcie tabeli `signals` – żeby raport liczył ten sam `summarize` co `stats`."""
        o = self.outcome
        return {"status": o.status.value, "assessment": json.dumps(asdict(self.signal.assessment)),
                "close_reason": o.reason.value if o.reason else None,
                "result_r": o.result_r, "result_pct": o.result_pct}


@dataclass
class BacktestReport:
    news: int = 0
    decisions: Counter = field(default_factory=Counter)   # etap lejka -> liczba
    signals: list[BacktestSignal] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        rows = [s.row() for s in self.signals]
        by_source: dict[str, list[dict]] = {}
        for s, r in zip(self.signals, rows, strict=True):
            by_source.setdefault(f"{s.kind}:{s.source}", []).append(r)
        base = summarize(rows)
        return {"news": self.news, "funnel": dict(sorted(self.decisions.items())),
                "all": base["all"], "by_catalyst": base["by_catalyst"],
                "by_source": {k: summarize(v)["all"] for k, v in sorted(by_source.items())}}


def _stage(decision: str) -> str:
    """'PKN: odrzucony – score' -> 'odrzucony'; 'PKN: SYGNAŁ score=…' -> 'sygnał'."""
    tail = decision.split(": ", 1)[-1]
    if tail.startswith("SYGNAŁ"):
        return "sygnał"
    return tail.split(" – ")[0].split(" (")[0].strip()


def backtest_news(settings: Settings, tickers: TickerMap, prices: PriceProvider, assessor: Assessor | None,
                  news: list[NewsItem], start: date | None = None, end: date | None = None,
                  calendar: EventCalendar | None = None,
                  make_assessor=None) -> BacktestReport:
    """Przepuszcza newsy (chronologicznie) przez pipeline. `assessor` albo `make_assessor(ToolContext)` –
    ten drugi dostaje notowania przycięte do chwili newsa, więc narzędzia agenta też nie widzą przyszłości."""
    asof = AsOfPrices(prices)
    cal = calendar or EventCalendar("__brak__.yaml")
    app = App(settings, Database(":memory:"), tickers, asof, cal, [], _SilentPublisher(), dry_run=True)
    if make_assessor is not None:
        from .agent.tools import ToolContext
        assessor = make_assessor(ToolContext(asof, tickers, app.db, cal))
    app.assessor = assessor
    rep = BacktestReport()
    tz = app.tz
    for n in sorted(news, key=lambda x: x.published_at):
        day = market_date(n.published_at, tz)
        if (start and day < start) or (end and day > end):
            continue
        rep.news += 1
        app.clock = lambda t=n.published_at: t
        asof.as_of = n.published_at
        n.tickers = tickers.map_news(n.title, n.summary, n.kind)
        if not n.tickers:
            rep.decisions["brak tickera"] += 1
            continue
        if app.signals_today() >= settings.filters.max_signals_per_day:
            rep.decisions["limit dzienny"] += 1
            continue
        regime = app._regime()
        for tk in n.tickers[:2]:
            try:
                sig, decision = app.process(n, tk, regime)
            except TransientError as e:
                sig, decision = None, f"{tk}: błąd danych – {e}"
            rep.decisions[_stage(decision)] += 1
            rep.details.append(f"{day} {decision} | {n.title}")
            if sig is None:
                continue
            company = tickers.get(tk)
            # Wynik na PEŁNEJ historii: evaluate bierze wyłącznie sesje po dniu newsa
            out = evaluate(sig, asof.full(company.symbol), tz, settings.tracker.breakeven_after_t1)
            rep.signals.append(BacktestSignal(sig, n.source, n.kind.value, out))
    return rep
