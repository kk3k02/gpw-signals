"""Harmonogram: polling newsów w godzinach aktywności w dni sesyjne + tracker po sesji."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from .config import ScheduleConfig
from .monitor import write_heartbeat
from .pipeline import App

log = logging.getLogger(__name__)


def is_session_day(d: date, holidays: list[str]) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays


def in_active_window(now: datetime, start: str, end: str) -> bool:
    return time.fromisoformat(start) <= now.time() <= time.fromisoformat(end)


def should_poll(now: datetime, cfg: ScheduleConfig) -> bool:
    return is_session_day(now.date(), cfg.holidays) and in_active_window(now, cfg.active_from, cfg.active_to)


def missing_holiday_years(today: date, holidays: list[str]) -> list[int]:
    """Lata bez wpisanych dni bez sesji: bieżący, a od grudnia także następny."""
    years = [today.year, today.year + 1] if today.month == 12 else [today.year]
    return [y for y in years if not any(h.startswith(f"{y}-") for h in holidays)]


def make_scheduler(tz: ZoneInfo) -> BlockingScheduler:
    """Jeden wątek roboczy: zadania nie nachodzą na siebie (wspólne połączenie SQLite).
    Zadanie opóźnione przez poprzednie (np. długi import notowań) wykona się później, a nie przepadnie."""
    return BlockingScheduler(timezone=tz, executors={"default": ThreadPoolExecutor(1)},
                             job_defaults={"coalesce": True, "misfire_grace_time": 3600})


def run_job(app: App, what: str, job: Callable[[], object], threshold: int | None = None) -> bool:
    """Wykonuje zadanie; wyjątek trafia do logu i monitoringu, a nie przerywa schedulera."""
    try:
        job()
    except Exception as e:
        log.exception("Błąd: %s", what)
        app.monitor.fail(what, f"{type(e).__name__}: {e}", threshold)
        return False
    app.monitor.ok(what)
    return True


def poll_tick(app: App, now: datetime) -> None:
    """Znacznik życia po udanym cyklu albo poza godzinami pracy (bot działa, tylko czeka)."""
    if should_poll(now, app.settings.schedule) and not run_job(
            app, "cykl", lambda: log.info("Cykl zakończony, sygnałów: %d", len(app.run_once()))):
        return
    try:
        write_heartbeat(app.settings.monitoring.heartbeat_file, app.clock())
    except OSError as e:
        log.warning("Znacznik życia niezapisany: %s", e)


def run_forever(app: App) -> None:
    s = app.settings
    tz = ZoneInfo(s.timezone)
    sched = make_scheduler(tz)

    def session_job(what: str, job: Callable[[], object]) -> Callable[[], None]:
        def wrapped() -> None:
            if is_session_day(datetime.now(tz).date(), s.schedule.holidays):
                run_job(app, what, job, threshold=1)   # zadania raz dziennie – alert od razu
        return wrapped

    def poll() -> None:
        poll_tick(app, datetime.now(tz))

    def weekly() -> None:
        run_job(app, "podsumowanie tygodnia", app.weekly_summary, threshold=1)

    track = session_job("tracker", app.track)
    prices = session_job("import notowań", app.sync_prices)
    report = session_job("raport dzienny", app.daily_report)

    for year in missing_holiday_years(datetime.now(tz).date(), s.schedule.holidays):
        log.warning("Brak dni bez sesji na rok %d – uzupełnij schedule.holidays", year)

    # Na starcie dociągamy zaległe notowania (np. po przerwie w działaniu bota)
    run_job(app, "import notowań", app.sync_prices, threshold=1)

    ph, pm = s.schedule.prices_at.split(":")
    sched.add_job(prices, "cron", day_of_week="mon-fri", hour=int(ph), minute=int(pm))
    hh, mm = s.schedule.tracker_at.split(":")
    sched.add_job(poll, "interval", minutes=s.schedule.poll_minutes, next_run_time=datetime.now(tz),
                  max_instances=1, coalesce=True)
    sched.add_job(track, "cron", day_of_week="mon-fri", hour=int(hh), minute=int(mm))
    if s.monitoring.daily_report:
        rh, rm = s.monitoring.daily_report_at.split(":")
        sched.add_job(report, "cron", day_of_week="mon-fri", hour=int(rh), minute=int(rm))
    if s.tracker.weekly_summary:
        wh, wm = s.tracker.weekly_summary_at.split(":")
        sched.add_job(weekly, "cron", day_of_week=s.tracker.weekly_summary_day, hour=int(wh), minute=int(wm))
    log.info("Start schedulera (co %d min, %s–%s)", s.schedule.poll_minutes,
             s.schedule.active_from, s.schedule.active_to)
    sched.start()
