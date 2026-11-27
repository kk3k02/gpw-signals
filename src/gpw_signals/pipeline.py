"""Orkiestracja: newsy -> ticker -> agent -> poziomy -> filtry -> publikacja; oraz tracker wyników."""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

from .agent.base import AgentError, AgentUnavailable, Assessor
from .agent.factory import make_assessor
from .agent.tools import ToolContext
from .clock import market_date, market_day_bounds, utc_now
from .config import Settings
from .db import Database
from .ingest.base import Source, fetch_all
from .ingest.macro import EventCalendar
from .ingest.report_schedule import ReportCalendar, ScheduleError, extract_body, fetch_page, is_report_schedule
from .ingest.rss_portals import RssSource, needs_body
from .levels.engine import compute_levels
from .levels.validate import validate_levels
from .mapping import TickerMap
from .market import gpw_archive
from .market.indicators import snapshot
from .market.intraday import IntradayProvider, NoIntraday, SessionPhase, fresh_quote, make_intraday, session_phase
from .market.prices import PriceProvider, make_provider
from .market.quality import data_issues
from .market.regime import Regime, market_regime
from .models import Assessment, NewsItem, Signal, SignalStatus, SourceKind
from .monitor import Monitor, format_daily_report
from .prefilter import noise_reason
from .publish.formatter import conflict_note_for, format_outcome, format_signal, format_weekly_summary
from .publish.telegram import DryRunPublisher, Publisher, TelegramPublisher
from .scoring import pre_agent_check, score_signal
from .simulation import simulate_target_first
from .tracker.outcomes import evaluate
from .tracker.stats import summarize

log = logging.getLogger(__name__)


class TransientError(Exception):
    """Błąd przejściowy (sieć, API, brak notowań): news zostaje w kolejce do ponowienia."""


def _is_transient_api_error(e: Exception) -> bool:
    if isinstance(e, AgentUnavailable | anthropic.APIConnectionError | anthropic.RateLimitError):
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
    assessor: Assessor | None = None
    dry_run: bool = False
    # Źródło czasu (aware UTC) – podmieniane w testach
    clock: Callable[[], datetime] = field(default=utc_now)
    # Pobieranie strony komunikatu (terminy raportów) – podmieniane w testach
    fetch_page: Callable[[str], str] = field(default=fetch_page)
    monitor: Monitor = field(default_factory=Monitor)
    # Bieżący kurs w sesji (#4); brak = świece dzienne
    intraday: IntradayProvider = field(default_factory=NoIntraday)
    # Statystyki ostatniego cyklu: wywołania LLM i decyzje podjęte bez LLM, wg etapu (#5)
    cycle_stats: Counter = field(default_factory=Counter)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def today(self) -> date:
        return market_date(self.clock(), self.tz)

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
        calendar = EventCalendar(settings.sources.macro_calendar_file, ZoneInfo(settings.timezone),
                                 extra=[settings.sources.report_calendar_file])
        sources: list[Source] = [RssSource(f, settings.sources.broker_keywords) for f in settings.sources.rss
                                   if f.enabled]
        sec = settings.secrets
        if dry_run or not settings.publish.enabled:
            publisher: Publisher = DryRunPublisher()
        else:
            chat = sec.telegram_test_channel_id if settings.publish.use_test_channel else sec.telegram_channel_id
            publisher = TelegramPublisher(sec.telegram_bot_token, chat)
        app = cls(settings, db, tickers, prices, calendar, sources, publisher, dry_run=dry_run)
        app.monitor = Monitor(threshold=settings.monitoring.fail_threshold)
        app.intraday = make_intraday(m.intraday_provider, m.intraday_dir)
        if not dry_run and sec.telegram_bot_token and sec.telegram_admin_chat_id:
            app.monitor.notify = TelegramPublisher(sec.telegram_bot_token, sec.telegram_admin_chat_id).send
        app.assessor = make_assessor(settings.agent, ToolContext(prices, tickers, db, calendar))
        app.assessor.on_usage = lambda model, u: db.add_usage(app.today(), model, u)
        return app

    # ---------------------------------------------------------------- newsy
    def run_once(self) -> list[Signal]:
        """Jeden cykl. Przetwarza WSZYSTKIE nieprzetworzone newsy z bazy – także zaległe po crashu
        lub błędzie przejściowym – a nie tylko te pobrane w tym cyklu (#1)."""
        self.cycle_stats = Counter()
        self.publish_pending()

        news = fetch_all(self.sources, self._source_result)
        pc = self.settings.processing
        max_age = timedelta(hours=pc.max_news_age_hours)
        now = self.clock()
        new, body_fetches = 0, 0
        for n in news:
            if now - n.published_at > max_age:
                # Odrzucamy już przy pobraniu: stary news nie trafia do bazy ani kolejki (#7)
                self.cycle_stats["news:za stary"] += 1
                continue
            if self._wants_body(n) and body_fetches < self.settings.sources.max_body_fetches:
                body_fetches += 1
                self._fill_body(n)
            n.tickers = self.tickers.map_news(n.title, n.summary, n.kind)
            new += self.db.insert_news_if_new(n)
        if new:
            self.cycle_stats["news:nowe"] = new
        queue = self.db.pending_news()
        log.info("Nowych newsów: %d (z %d), w kolejce: %d", new, len(news), len(queue))

        regime = self._regime()
        published: list[Signal] = []
        for n in queue:
            if now - n.published_at > max_age:
                self.db.mark_news(n.uid, "nieaktualny")
                self.cycle_stats["news:nieaktualny"] += 1
                continue
            if not n.tickers:
                self.db.mark_news(n.uid, "brak tickera")
                self.cycle_stats["news:brak tickera"] += 1
                continue
            if self._is_schedule(n):
                try:
                    self.db.mark_news(n.uid, self._import_schedule(n))
                    self.cycle_stats["news:terminy raportów"] += 1
                except Exception as e:  # noqa: BLE001 – sieć/plik: ponowienie do limitu prób
                    log.warning("Terminy raportów %s: %s", n.tickers[0], e)
                    self.db.record_failure(n.uid, str(e), pc.max_attempts)
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
        self.db.add_stage_counts(self.today(), self.cycle_stats)
        return published

    def _source_result(self, name: str, error: Exception | None) -> None:
        if error is None:
            self.monitor.ok(f"źródło {name}")
        else:
            self.monitor.fail(f"źródło {name}", error)

    def _assess(self, n: NewsItem, ticker: str, company: str) -> Assessment:
        try:
            a = self.assessor.assess(n, ticker, company)
        except Exception as e:
            self.monitor.fail("agent", f"{type(e).__name__}: {e}")
            raise
        self.monitor.ok("agent")
        return a

    def _wants_body(self, n: NewsItem) -> bool:
        return (self.settings.sources.fetch_espi_body and n.kind == SourceKind.ESPI and bool(n.url)
                and needs_body(n) and not self.db.has_news(n.uid))

    def _fill_body(self, n: NewsItem) -> None:
        """Treść komunikatu ze strony, gdy RSS ma tylko tytuł. Błąd = zostaje to, co dał RSS."""
        try:
            body = extract_body(self.fetch_page(n.url))
        except Exception as e:  # noqa: BLE001 – sieć: news idzie dalej z samym tytułem
            log.warning("Treść komunikatu %s: %s", n.url, e)
            return
        if body:
            n.summary = body[:2000]
            self.cycle_stats["news:pobrana treść"] += 1

    def _is_schedule(self, n: NewsItem) -> bool:
        # Dry-run ma nie zostawiać śladów, a ten import zapisuje plik
        return (self.settings.sources.fetch_report_schedules and not self.dry_run
                and n.kind == SourceKind.ESPI and is_report_schedule(n.title))

    def _import_schedule(self, n: NewsItem) -> str:
        """Komunikat z terminami raportów trafia do kalendarza, a nie do oceny przez LLM."""
        ticker = n.tickers[0]
        body = extract_body(self.fetch_page(n.url))
        try:
            dates = ReportCalendar(self.settings.sources.report_calendar_file).import_announcement(
                ticker, n.title, body, market_date(n.published_at, self.tz))
        except ScheduleError as e:
            log.warning("Terminy raportów %s: %s (%s)", ticker, e, n.url)
            return f"{ticker}: terminy raportów – {e}"
        self.calendar.reload()
        return f"{ticker}: terminy raportów – zapisano {len(dates)}"

    def process(self, n: NewsItem, ticker: str, regime: Regime) -> tuple[Signal | None, str]:
        """Ocena newsa dla jednej spółki. Zwraca (sygnał | None, decyzja).
        Rzuca TransientError, gdy decyzji nie da się podjąć teraz, ale warto spróbować ponownie."""
        f = self.settings.filters
        company = self.tickers.get(ticker)
        if company is None:
            return None, f"{ticker}: nieznany ticker"
        noise = noise_reason(n, self.settings.prefilter)
        if noise:
            return self._skip("szum", f"{ticker}: szum – {noise}")
        if self.db.has_recent_signal(ticker, self.clock() - timedelta(days=f.cooldown_days)):
            return self._skip("cooldown", f"{ticker}: cooldown")
        try:
            df = self.prices.daily(company.symbol, self.settings.market.history_days)
        except Exception as e:
            self.monitor.fail("notowania", f"{ticker}: {e}")
            raise TransientError(f"{ticker}: brak notowań ({e})") from e
        self.monitor.ok("notowania")
        m = self.settings.market
        issues = data_issues(df, market_date(self.clock(), self.tz), m.max_data_age_days, m.max_jump_pct)
        if issues:
            return self._skip("dane", f"{ticker}: dane – {'; '.join(issues)}")
        live = self._live_price(company.symbol)
        try:
            snap = snapshot(df, self.settings.levels.atr_period,
                            self.settings.levels.swing_window, self.settings.levels.swing_lookback,
                            live_price=live, session_date=market_date(self.clock(), self.tz))
        except ValueError as e:
            return self._skip("historia", f"{ticker}: {e}")
        phase = session_phase(n.published_at, self.tz, m.session_open, m.session_close)

        # #5: wszystko, co nie zależy od oceny agenta, sprawdzamy PRZED kosztownym wywołaniem LLM
        lr = compute_levels(snap, self.settings.levels, company.liquidity_band)
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
            a = self._assess(n, ticker, company.name)
        except AgentError as e:  # odmowa, przekroczone limity – ponowienie nic nie da
            log.warning("Agent %s: %s", ticker, e)
            self.cycle_stats["po_llm:błąd agenta"] += 1
            return None, f"{ticker}: agent – {e}"
        except Exception as e:
            if _is_transient_api_error(e):
                raise TransientError(f"{ticker}: model ({type(e).__name__}: {e})") from e
            raise

        sr = score_signal(a, snap, lr.levels, regime, n.kind, f,
                          earnings_soon=self.calendar.earnings_within(
                              ticker, today=market_date(self.clock(), self.tz)))
        if not sr.passed:
            log.info("%s odrzucony: %s", ticker, sr.rejections)
            self.cycle_stats["po_llm:odrzucony"] += 1
            return None, f"{ticker}: odrzucony – {'; '.join(sr.rejections)}"

        mc = self.settings.simulation
        sim_note = ""
        if mc.enabled:
            try:
                sim = simulate_target_first(df, lr.levels, a.horizon_days, target_index=mc.target - 1,
                                            n_paths=mc.n_paths, lookback_days=mc.lookback_days, seed=mc.seed)
            except ValueError as e:
                self.cycle_stats["po_llm:symulacja"] += 1
                return None, f"{ticker}: symulacja – {e}"
            sim_note = f", T{mc.target} przed SL {sim.probability:.0%} z {sim.n_paths} symulacji"
            if sim.probability < mc.min_probability:
                log.info("%s odrzucony przez symulację: %.1f%%", ticker, sim.probability * 100)
                self.cycle_stats["po_llm:symulacja"] += 1
                return None, (f"{ticker}: symulacja – szansa T{mc.target} przed SL {sim.probability:.0%}"
                              f" < {mc.min_probability:.0%}")

        # #2: najpierw zapis (status pending_publish), dopiero potem wysyłka. Dzięki temu sygnał
        # od razu blokuje cooldown, a nieudana wysyłka jest ponawiana z bazy, nie przez ponowną ocenę newsa.
        sig = Signal(ticker=ticker, company_name=company.name, levels=lr.levels, score=sr.score,
                     assessment=a, news_uid=n.uid, created_at=self.clock(),
                     status=SignalStatus.PENDING_PUBLISH)
        self.db.insert_signal(sig)
        self.cycle_stats["sygnał"] += 1
        if self._publish(sig):
            return sig, f"{ticker}: SYGNAŁ score={sr.score:.2f} ({phase.value}, kurs {snap.price_source}{sim_note})"
        return sig, f"{ticker}: SYGNAŁ score={sr.score:.2f} ({phase.value}{sim_note}) – publikacja do ponowienia"

    def _live_price(self, symbol: str) -> float | None:
        """Bieżący kurs tylko w trakcie sesji i tylko świeży; inaczej None = świece dzienne (#4).
        News przed sesją (ESPI 7:00–9:00) jest więc oceniany od wczorajszego zamknięcia."""
        m = self.settings.market
        now = self.clock()
        if session_phase(now, self.tz, m.session_open, m.session_close) != SessionPhase.IN:
            return None
        try:
            q = fresh_quote(self.intraday, symbol, now, self.tz, m.intraday_max_age_min, m.session_open)
        except Exception as e:  # noqa: BLE001 – awaria intraday nie blokuje oceny
            self.monitor.fail("kurs intraday", f"{symbol}: {e}")
            return None
        if q is None:
            log.debug("%s: brak świeżego kursu intraday – świece dzienne", symbol)
        return q.price if q else None

    def _skip(self, stage: str, decision: str) -> tuple[None, str]:
        """Decyzja podjęta bez wywołania LLM – liczona, żeby było widać oszczędność."""
        self.cycle_stats[f"bez_llm:{stage}"] += 1
        return None, decision

    def _log_cycle_stats(self) -> None:
        st = self.cycle_stats
        skipped = sum(v for k, v in st.items() if k.startswith("bez_llm:"))
        detail = ", ".join(f"{k.removeprefix('bez_llm:')}={v}" for k, v in sorted(st.items())
                           if k not in ("llm", "news:nowe"))
        log.info("Wywołania LLM: %d, decyzje bez LLM: %d%s", st["llm"], skipped, f" ({detail})" if detail else "")

    # ---------------------------------------------------------------- publikacja
    def _publish(self, sig: Signal) -> bool:
        p = self.settings.publish
        try:
            conflict = conflict_note_for(sig.ticker, p.conflict_tickers, p.conflict_note)
            msg_id = self.publisher.send(format_signal(sig, p.include_thesis, p.disclaimer,
                                                       p.methodology_url, conflict))
        except Exception as e:  # noqa: BLE001 – każda awaria wysyłki = ponowienie z bazy
            log.warning("Publikacja sygnału %s (%s) nieudana: %s", sig.id, sig.ticker, e)
            self.monitor.fail("publikacja", e, threshold=1)
            return False
        self.monitor.ok("publikacja")
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
            regime = market_regime(self.prices.daily(self.settings.market.index_symbol, 260))
            self.monitor.ok("notowania indeksu")
            return regime
        except Exception as e:  # noqa: BLE001
            self.monitor.fail("notowania indeksu", e)
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

    def daily_report(self) -> None:
        """Raport dnia na czat admina (bez czatu: do logów)."""
        day = self.today().isoformat()
        report = next((r for r in self.db.daily_report(limit=1) if r["day"] == day), None)
        self.monitor.send(format_daily_report(day, report, self.signals_today()))

    def weekly_summary(self) -> bool:
        """Podsumowanie sygnałów zamkniętych w ostatnich 7 dniach. False = nie było czego wysłać."""
        rows = self.db.closed_signals_since(self.clock() - timedelta(days=7))
        if not rows:
            return False
        self.publisher.send(format_weekly_summary(summarize(rows)["all"]))
        return True
