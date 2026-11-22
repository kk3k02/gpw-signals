"""Orkiestracja: newsy -> ticker -> agent -> poziomy -> filtry -> publikacja; oraz tracker wyników."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anthropic

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


class TransientError(Exception):
    """Błąd przejściowy (sieć, API, brak notowań): news zostaje w kolejce do ponowienia."""


def _is_transient_api_error(e: Exception) -> bool:
    if isinstance(e, anthropic.APIConnectionError | anthropic.RateLimitError):
        return True
    return isinstance(e, anthropic.APIStatusError) and e.status_code >= 500


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
        """Jeden cykl. Przetwarza WSZYSTKIE nieprzetworzone newsy z bazy – także zaległe po crashu
        lub błędzie przejściowym – a nie tylko te pobrane w tym cyklu (#1)."""
        self.publish_pending()

        news = fetch_all(self.sources)
        new = 0
        for n in news:
            n.tickers = self.tickers.find(f"{n.title} {n.summary}")
            new += self.db.insert_news_if_new(n)
        queue = self.db.pending_news()
        log.info("Nowych newsów: %d (z %d), w kolejce: %d", new, len(news), len(queue))

        regime = self._regime()
        pc = self.settings.processing
        max_age = timedelta(hours=pc.max_news_age_hours)
        now = datetime.now(UTC)
        published: list[Signal] = []
        for n in queue:
            if now - n.published_at > max_age:
                self.db.mark_news(n.uid, "nieaktualny")
                continue
            if not n.tickers:
                self.db.mark_news(n.uid, "brak tickera")
                continue
            if self.db.signals_today() >= self.settings.filters.max_signals_per_day:
                log.info("Osiągnięto dzienny limit sygnałów – reszta kolejki czeka")
                break

            decisions: list[str] = []
            try:
                for tk in n.tickers[:2]:  # news o wielu spółkach – oceniamy maks. 2 pierwsze
                    sig, decision = self.process(n, tk, regime)
                    decisions.append(decision)
                    if sig and sig.status == SignalStatus.ACTIVE:
                        published.append(sig)
            except TransientError as e:
                # Bez decyzji ostatecznej: news zostaje w kolejce do limitu prób. Spółka, dla której
                # sygnał już powstał, przy ponowieniu zatrzyma się na cooldownie – bez duplikatu.
                if self.db.record_failure(n.uid, str(e), pc.max_attempts):
                    log.warning("News %s porzucony po %d próbach: %s", n.uid, pc.max_attempts, e)
                else:
                    log.info("News %s do ponowienia: %s", n.uid, e)
                continue
            except Exception as e:
                # Nieprzewidziany błąd: nie zatrzymujemy cyklu, a limit prób chroni przed "trującym" newsem
                log.exception("Nieoczekiwany błąd przy newsie %s", n.uid)
                self.db.record_failure(n.uid, f"{type(e).__name__}: {e}", pc.max_attempts)
                continue
            self.db.mark_news(n.uid, " | ".join(decisions))
        return published

    def process(self, n: NewsItem, ticker: str, regime: Regime) -> tuple[Signal | None, str]:
        """Ocena newsa dla jednej spółki. Zwraca (sygnał | None, decyzja).
        Rzuca TransientError, gdy decyzji nie da się podjąć teraz, ale warto spróbować ponownie."""
        f = self.settings.filters
        company = self.tickers.get(ticker)
        if company is None:
            return None, f"{ticker}: nieznany ticker"
        if self.db.has_recent_signal(ticker, f.cooldown_days):
            return None, f"{ticker}: cooldown"
        try:
            df = self.prices.daily(company.stooq_symbol, self.settings.market.history_days)
        except Exception as e:
            raise TransientError(f"{ticker}: brak notowań ({e})") from e
        try:
            snap = snapshot(df, self.settings.levels.atr_period,
                            self.settings.levels.swing_window, self.settings.levels.swing_lookback)
        except ValueError as e:
            return None, f"{ticker}: {e}"

        # Tani pre-filtr przed kosztownym wywołaniem LLM
        if snap.avg_turnover_20 < f.min_avg_turnover_pln:
            return None, f"{ticker}: niska płynność"

        try:
            a = self.assessor.assess(n, ticker, company.name)
        except AgentError as e:  # odmowa, przekroczone limity – ponowienie nic nie da
            log.warning("Agent %s: %s", ticker, e)
            return None, f"{ticker}: agent – {e}"
        except Exception as e:
            if _is_transient_api_error(e):
                raise TransientError(f"{ticker}: API Claude ({type(e).__name__}: {e})") from e
            raise

        lr = compute_levels(snap, self.settings.levels)
        if lr.levels is None:
            return None, f"{ticker}: poziomy – {lr.reason}"
        errors = validate_levels(lr.levels, f.min_rr_t1)
        if errors:
            return None, f"{ticker}: walidacja – {'; '.join(errors)}"

        sr = score_signal(a, snap, lr.levels, regime, n.kind, f,
                          earnings_soon=self.calendar.earnings_within(ticker))
        if not sr.passed:
            log.info("%s odrzucony: %s", ticker, sr.rejections)
            return None, f"{ticker}: odrzucony – {'; '.join(sr.rejections)}"

        # #2: najpierw zapis (status pending_publish), dopiero potem wysyłka. Dzięki temu sygnał
        # od razu blokuje cooldown, a nieudana wysyłka jest ponawiana z bazy, nie przez ponowną ocenę newsa.
        sig = Signal(ticker=ticker, company_name=company.name, levels=lr.levels, score=sr.score,
                     assessment=a, news_uid=n.uid, created_at=datetime.now(),
                     status=SignalStatus.PENDING_PUBLISH)
        self.db.insert_signal(sig)
        if self._publish(sig):
            return sig, f"{ticker}: SYGNAŁ score={sr.score:.2f}"
        return sig, f"{ticker}: SYGNAŁ score={sr.score:.2f} – publikacja do ponowienia"

    # ---------------------------------------------------------------- publikacja
    def _publish(self, sig: Signal) -> bool:
        p = self.settings.publish
        try:
            msg_id = self.publisher.send(format_signal(sig, p.include_thesis, p.disclaimer))
        except Exception as e:  # noqa: BLE001 – każda awaria wysyłki = ponowienie z bazy
            log.warning("Publikacja sygnału %s (%s) nieudana: %s", sig.id, sig.ticker, e)
            return False
        self.db.mark_published(sig.id, msg_id)
        sig.telegram_message_id, sig.status = msg_id, SignalStatus.ACTIVE
        return True

    def publish_pending(self) -> int:
        """Ponawia wysyłkę zapisanych, a nieopublikowanych sygnałów. Zbyt stare nie są już wysyłane."""
        limit = timedelta(minutes=self.settings.publish.max_pending_minutes)
        sent = 0
        for s in self.db.pending_signals():
            if datetime.now() - s.created_at > limit:
                log.warning("Sygnał %s (%s) nieopublikowany w %s – porzucony", s.id, s.ticker, limit)
                self.db.update_signal_status(s.id, SignalStatus.UNPUBLISHED, closed=True)
            elif self._publish(s):
                sent += 1
        return sent

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
