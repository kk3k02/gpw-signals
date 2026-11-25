"""Orkiestracja: newsy -> ticker -> agent -> poziomy -> filtry -> publikacja; oraz tracker wyników."""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

from .agent.assess import AgentError, NewsAssessor
from .agent.tools import ToolContext
from .clock import market_date, market_day_bounds, utc_now
from .config import Settings
from .db import Database
from .ingest.base import Source, fetch_all
from .ingest.macro import EventCalendar
from .ingest.rss_portals import RssSource
from .levels.engine import compute_levels
from .levels.validate import validate_levels
from .mapping import TickerMap
from .market import gpw_archive
from .market.indicators import snapshot
from .market.prices import PriceProvider, make_provider
from .market.quality import data_issues
from .market.regime import Regime, market_regime
from .models import NewsItem, Signal, SignalStatus
from .publish.formatter import format_outcome, format_signal, format_weekly_summary
from .publish.telegram import DryRunPublisher, Publisher, TelegramPublisher
from .scoring import pre_agent_check, score_signal
from .tracker.outcomes import evaluate
from .tracker.stats import summarize

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
    # Źródło czasu (aware UTC) – podmieniane w testach
    clock: Callable[[], datetime] = field(default=utc_now)
    # Statystyki ostatniego cyklu: wywołania LLM i decyzje podjęte bez LLM, wg etapu (#5)
    cycle_stats: Counter = field(default_factory=Counter)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def signals_today(self) -> int:
        """Sygnały z bieżącego dnia w strefie rynku – niezależnie od strefy serwera."""
        return self.db.count_signals_between(*market_day_bounds(market_date(self.clock(), self.tz), self.tz))

    # ---------------------------------------------------------------- budowa
    @classmethod
    def build(cls, settings: Settings, dry_run: bool = False) -> App:
        # Dry-run na bazie w pamięci: nic nie zapisujemy, więc kolejne prawdziwe uruchomienie zobaczy te same newsy
        db = Database(":memory:" if dry_run else settings.db_path)
        tickers = TickerMap.from_csv(settings.tickers_csv)
        m = settings.market
        prices = make_provider(m.provider, m.stooq_url, m.csv_dir)
        calendar = EventCalendar(settings.sources.macro_calendar_file, ZoneInfo(settings.timezone))
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
        self.cycle_stats = Counter()
        self.publish_pending()

        news = fetch_all(self.sources)
        new = 0
        for n in news:
            n.tickers = self.tickers.map_news(n.title, n.summary, n.kind)
            new += self.db.insert_news_if_new(n)
        queue = self.db.pending_news()
        log.info("Nowych newsów: %d (z %d), w kolejce: %d", new, len(news), len(queue))

        regime = self._regime()
        pc = self.settings.processing
        max_age = timedelta(hours=pc.max_news_age_hours)
        now = self.clock()
        published: list[Signal] = []
        for n in queue:
            if now - n.published_at > max_age:
                self.db.mark_news(n.uid, "nieaktualny")
                continue
            if not n.tickers:
                self.db.mark_news(n.uid, "brak tickera")
                continue
            if self.signals_today() >= self.settings.filters.max_signals_per_day:
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
        self._log_cycle_stats()
        return published

    def process(self, n: NewsItem, ticker: str, regime: Regime) -> tuple[Signal | None, str]:
        """Ocena newsa dla jednej spółki. Zwraca (sygnał | None, decyzja).
        Rzuca TransientError, gdy decyzji nie da się podjąć teraz, ale warto spróbować ponownie."""
        f = self.settings.filters
        company = self.tickers.get(ticker)
        if company is None:
            return None, f"{ticker}: nieznany ticker"
        if self.db.has_recent_signal(ticker, self.clock() - timedelta(days=f.cooldown_days)):
            return self._skip("cooldown", f"{ticker}: cooldown")
        try:
            df = self.prices.daily(company.symbol, self.settings.market.history_days)
        except Exception as e:
            raise TransientError(f"{ticker}: brak notowań ({e})") from e
        m = self.settings.market
        issues = data_issues(df, market_date(self.clock(), self.tz), m.max_data_age_days, m.max_jump_pct)
        if issues:
            return self._skip("dane", f"{ticker}: dane – {'; '.join(issues)}")
        try:
            snap = snapshot(df, self.settings.levels.atr_period,
                            self.settings.levels.swing_window, self.settings.levels.swing_lookback)
        except ValueError as e:
            return self._skip("historia", f"{ticker}: {e}")

        # #5: wszystko, co nie zależy od oceny agenta, sprawdzamy PRZED kosztownym wywołaniem LLM
        lr = compute_levels(snap, self.settings.levels)
        if lr.levels is None:
            return self._skip("poziomy", f"{ticker}: poziomy – {lr.reason}")
        errors = validate_levels(lr.levels, f.min_rr_t1)
        if errors:
            return self._skip("walidacja", f"{ticker}: walidacja – {'; '.join(errors)}")
        pre = pre_agent_check(snap, lr.levels, regime, n.kind, f)
        if pre:
            return self._skip("filtry", f"{ticker}: odrzucony przed agentem – {'; '.join(pre)}")

        self.cycle_stats["llm"] += 1
        try:
            a = self.assessor.assess(n, ticker, company.name)
        except AgentError as e:  # odmowa, przekroczone limity – ponowienie nic nie da
            log.warning("Agent %s: %s", ticker, e)
            return None, f"{ticker}: agent – {e}"
        except Exception as e:
            if _is_transient_api_error(e):
                raise TransientError(f"{ticker}: API Claude ({type(e).__name__}: {e})") from e
            raise

        sr = score_signal(a, snap, lr.levels, regime, n.kind, f,
                          earnings_soon=self.calendar.earnings_within(
                              ticker, today=market_date(self.clock(), self.tz)))
        if not sr.passed:
            log.info("%s odrzucony: %s", ticker, sr.rejections)
            return None, f"{ticker}: odrzucony – {'; '.join(sr.rejections)}"

        # #2: najpierw zapis (status pending_publish), dopiero potem wysyłka. Dzięki temu sygnał
        # od razu blokuje cooldown, a nieudana wysyłka jest ponawiana z bazy, nie przez ponowną ocenę newsa.
        sig = Signal(ticker=ticker, company_name=company.name, levels=lr.levels, score=sr.score,
                     assessment=a, news_uid=n.uid, created_at=self.clock(),
                     status=SignalStatus.PENDING_PUBLISH)
        self.db.insert_signal(sig)
        if self._publish(sig):
            return sig, f"{ticker}: SYGNAŁ score={sr.score:.2f}"
        return sig, f"{ticker}: SYGNAŁ score={sr.score:.2f} – publikacja do ponowienia"

    def _skip(self, stage: str, decision: str) -> tuple[None, str]:
        """Decyzja podjęta bez wywołania LLM – liczona, żeby było widać oszczędność."""
        self.cycle_stats[f"bez_llm:{stage}"] += 1
        return None, decision

    def _log_cycle_stats(self) -> None:
        st = self.cycle_stats
        skipped = sum(v for k, v in st.items() if k.startswith("bez_llm:"))
        detail = ", ".join(f"{k.removeprefix('bez_llm:')}={v}" for k, v in sorted(st.items()) if k != "llm")
        log.info("Wywołania LLM: %d, decyzje bez LLM: %d%s", st["llm"], skipped, f" ({detail})" if detail else "")

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
            if self.clock() - s.created_at > limit:
                log.warning("Sygnał %s (%s) nieopublikowany w %s – porzucony", s.id, s.ticker, limit)
                self.db.update_signal_status(s.id, SignalStatus.UNPUBLISHED, closed_at=self.clock())
            elif self._publish(s):
                sent += 1
        return sent

    # ---------------------------------------------------------------- notowania
    def sync_prices(self, start: date | None = None, end: date | None = None) -> gpw_archive.ImportSummary:
        """Import notowań dziennych z archiwum GPW do csv_dir (tylko spółki z listy + indeksy)."""
        m = self.settings.market
        client = gpw_archive.GpwArchiveClient(m.raw_dir, m.gpw_archive_url, m.min_interval_s)
        store = gpw_archive.PriceStore(m.csv_dir)
        shares = {c.isin: c.symbol for c in self.tickers.by_isin().values()}
        return gpw_archive.sync(client, store, shares, m.index_symbols, market_date(self.clock(), self.tz),
                                m.history_days, self.settings.schedule.holidays, start, end)

    def _regime(self) -> Regime:
        try:
            return market_regime(self.prices.daily(self.settings.market.index_symbol, 260))
        except Exception as e:  # noqa: BLE001
            log.warning("Brak danych indeksu (%s) – reżim NEUTRAL", e)
            return Regime.NEUTRAL

    # ---------------------------------------------------------------- tracker
    def track(self) -> None:
        today = market_date(self.clock(), self.tz)
        for s in self.db.open_signals():
            company = self.tickers.get(s.ticker)
            try:
                bars = self.prices.daily(company.symbol, 60)
            except Exception as e:  # noqa: BLE001
                log.warning("Tracker %s: %s", s.ticker, e)
                continue
            out = evaluate(s, bars, self.tz, self.settings.tracker.breakeven_after_t1)
            if out.status == s.status and not out.closed:
                continue
            if self.db.outcome_recorded_on(s.id, today):
                continue  # jedna aktualizacja dziennie – zmianę zobaczy przebieg następnego dnia
            if (out.closed or out.status != SignalStatus.ACTIVE) and self.settings.publish.post_outcomes:
                try:
                    self.publisher.send(format_outcome(s, out), reply_to=s.telegram_message_id)
                except Exception as e:  # noqa: BLE001 – bez zapisu, więc ponowi kolejny przebieg
                    log.warning("Wynik sygnału %s (%s) nieopublikowany: %s", s.id, s.ticker, e)
                    continue
            self.db.record_outcome(s.id, out.status, today, self.clock() if out.closed else None,
                                   out.reason, out.result_r, out.result_pct)

    def weekly_summary(self) -> bool:
        """Podsumowanie sygnałów zamkniętych w ostatnich 7 dniach. False = nie było czego wysłać."""
        rows = self.db.closed_signals_since(self.clock() - timedelta(days=7))
        if not rows:
            return False
        self.publisher.send(format_weekly_summary(summarize(rows)["all"]))
        return True
