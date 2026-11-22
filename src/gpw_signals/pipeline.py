"""Orkiestracja: newsy -> ticker -> agent -> poziomy -> filtry -> publikacja; oraz tracker wyników."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .agent.assess import AgentError, NewsAssessor
from .agent.tools import ToolContext
from .config import Settings
from .db import Database
from .ingest.base import Source, fetch_all
from .ingest.macro import EventCalendar
from .ingest.rss_portals import RssSource
from .levels.engine import compute_levels
from .levels.validate import validate_levels
from .mapping import TickerMap
from .market.indicators import snapshot
from .market.prices import PriceProvider, make_provider
from .market.regime import Regime, market_regime
from .models import NewsItem, Signal, SignalStatus
from .publish.formatter import format_outcome, format_signal
from .publish.telegram import DryRunPublisher, Publisher, TelegramPublisher
from .scoring import score_signal
from .tracker.outcomes import evaluate

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    db: Database
    tickers: TickerMap
    prices: PriceProvider
    calendar: EventCalendar
    sources: list[Source]
    publisher: Publisher
    assessor: NewsAssessor | None = None

    # ---------------------------------------------------------------- budowa
    @classmethod
    def build(cls, settings: Settings, dry_run: bool = False) -> App:
        # Dry-run na bazie w pamięci: nic nie zapisujemy, więc kolejne prawdziwe uruchomienie zobaczy te same newsy
        db = Database(":memory:" if dry_run else settings.db_path)
        tickers = TickerMap.from_csv(settings.tickers_csv)
        m = settings.market
        prices = make_provider(m.provider, m.stooq_url, m.csv_dir)
        calendar = EventCalendar(settings.sources.macro_calendar_file)
        sources: list[Source] = [RssSource(f, settings.sources.broker_keywords) for f in settings.sources.rss]
        sec = settings.secrets
        if dry_run or not settings.publish.enabled:
            publisher: Publisher = DryRunPublisher()
        else:
            chat = sec.telegram_test_channel_id if settings.publish.use_test_channel else sec.telegram_channel_id
            publisher = TelegramPublisher(sec.telegram_bot_token, chat)
        app = cls(settings, db, tickers, prices, calendar, sources, publisher)
        app.assessor = NewsAssessor(settings.agent, ToolContext(prices, tickers, db, calendar))
        return app

    # ---------------------------------------------------------------- newsy
    def run_once(self) -> list[Signal]:
        news = fetch_all(self.sources)
        fresh: list[NewsItem] = []
        for n in news:
            n.tickers = self.tickers.find(f"{n.title} {n.summary}")
            if self.db.insert_news_if_new(n):
                fresh.append(n)
        log.info("Nowych newsów: %d (z %d)", len(fresh), len(news))

        regime = self._regime()
        published: list[Signal] = []
        for n in fresh:
            if not n.tickers:
                self.db.mark_news(n.uid, "brak tickera")
                continue
            for tk in n.tickers[:2]:  # news o wielu spółkach – oceniamy maks. 2 pierwsze
                if self.db.signals_today() >= self.settings.filters.max_signals_per_day:
                    log.info("Osiągnięto dzienny limit sygnałów")
                    return published
                sig = self.process(n, tk, regime)
                if sig:
                    published.append(sig)
        return published

    def process(self, n: NewsItem, ticker: str, regime: Regime) -> Signal | None:
        f = self.settings.filters
        company = self.tickers.get(ticker)
        if company is None:
            return None
        if self.db.has_recent_signal(ticker, f.cooldown_days):
            self.db.mark_news(n.uid, f"{ticker}: cooldown")
            return None
        try:
            df = self.prices.daily(company.stooq_symbol, self.settings.market.history_days)
            snap = snapshot(df, self.settings.levels.atr_period,
                            self.settings.levels.swing_window, self.settings.levels.swing_lookback)
        except Exception as e:  # noqa: BLE001
            self.db.mark_news(n.uid, f"{ticker}: brak notowań ({e})")
            return None

        # Tani pre-filtr przed kosztownym wywołaniem LLM
        if snap.avg_turnover_20 < f.min_avg_turnover_pln:
            self.db.mark_news(n.uid, f"{ticker}: niska płynność")
            return None

        try:
            a = self.assessor.assess(n, ticker, company.name)
        except (AgentError, Exception) as e:  # noqa: BLE001
            log.warning("Agent: %s", e)
            self.db.mark_news(n.uid, f"{ticker}: błąd agenta")
            return None

        lr = compute_levels(snap, self.settings.levels)
        if lr.levels is None:
            self.db.mark_news(n.uid, f"{ticker}: poziomy – {lr.reason}")
            return None
        errors = validate_levels(lr.levels, f.min_rr_t1)
        if errors:
            self.db.mark_news(n.uid, f"{ticker}: walidacja – {'; '.join(errors)}")
            return None

        sr = score_signal(a, snap, lr.levels, regime, n.kind, f,
                          earnings_soon=self.calendar.earnings_within(ticker))
        if not sr.passed:
            self.db.mark_news(n.uid, f"{ticker}: odrzucony – {'; '.join(sr.rejections)}")
            log.info("%s odrzucony: %s", ticker, sr.rejections)
            return None

        sig = Signal(ticker=ticker, company_name=company.name, levels=lr.levels, score=sr.score,
                     assessment=a, news_uid=n.uid, created_at=datetime.now())
        p = self.settings.publish
        sig.telegram_message_id = self.publisher.send(format_signal(sig, p.include_thesis, p.disclaimer))
        self.db.insert_signal(sig)
        self.db.mark_news(n.uid, f"{ticker}: SYGNAŁ score={sr.score:.2f}")
        return sig

    def _regime(self) -> Regime:
        try:
            return market_regime(self.prices.daily(self.settings.market.index_symbol, 260))
        except Exception as e:  # noqa: BLE001
            log.warning("Brak danych indeksu (%s) – reżim NEUTRAL", e)
            return Regime.NEUTRAL

    # ---------------------------------------------------------------- tracker
    def track(self) -> None:
        for s in self.db.open_signals():
            company = self.tickers.get(s.ticker)
            try:
                bars = self.prices.daily(company.stooq_symbol, 60)
            except Exception as e:  # noqa: BLE001
                log.warning("Tracker %s: %s", s.ticker, e)
                continue
            out = evaluate(s, bars)
            if out.status != s.status or out.closed:
                if out.status != s.status and out.status != SignalStatus.ACTIVE \
                        and self.settings.publish.post_outcomes:
                    self.publisher.send(format_outcome(s, out.status), reply_to=s.telegram_message_id)
                self.db.update_signal_status(s.id, out.status, closed=out.closed)
