"""Harmonogram: polling newsów w godzinach aktywności w dni sesyjne + tracker po sesji."""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import ScheduleConfig
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


def run_forever(app: App) -> None:
    s = app.settings
    tz = ZoneInfo(s.timezone)
    sched = BlockingScheduler(timezone=tz)

    def poll() -> None:
        now = datetime.now(tz)
        if not should_poll(now, s.schedule):
            return
        try:
            sigs = app.run_once()
            log.info("Cykl zakończony, sygnałów: %d", len(sigs))
        except Exception:
            log.exception("Błąd cyklu")

    def track() -> None:
        if is_session_day(datetime.now(tz).date(), s.schedule.holidays):
            try:
                app.track()
            except Exception:
                log.exception("Błąd trackera")

    def weekly() -> None:
        try:
            app.weekly_summary()
        except Exception:
            log.exception("Błąd podsumowania tygodnia")

    def prices() -> None:
        if is_session_day(datetime.now(tz).date(), s.schedule.holidays):
            try:
                app.sync_prices()
            except Exception:
                log.exception("Błąd importu notowań")

    for year in missing_holiday_years(datetime.now(tz).date(), s.schedule.holidays):
        log.warning("Brak dni bez sesji na rok %d – uzupełnij schedule.holidays", year)

    # Na starcie dociągamy zaległe notowania (np. po przerwie w działaniu bota)
    try:
        app.sync_prices()
    except Exception:
        log.exception("Błąd importu notowań przy starcie")

    ph, pm = s.schedule.prices_at.split(":")
    sched.add_job(prices, "cron", day_of_week="mon-fri", hour=int(ph), minute=int(pm))
    hh, mm = s.schedule.tracker_at.split(":")
    sched.add_job(poll, "interval", minutes=s.schedule.poll_minutes, next_run_time=datetime.now(tz),
                  max_instances=1, coalesce=True)
    sched.add_job(track, "cron", day_of_week="mon-fri", hour=int(hh), minute=int(mm))
    if s.tracker.weekly_summary:
        wh, wm = s.tracker.weekly_summary_at.split(":")
        sched.add_job(weekly, "cron", day_of_week=s.tracker.weekly_summary_day, hour=int(wh), minute=int(wm))
    log.info("Start schedulera (co %d min, %s–%s)", s.schedule.poll_minutes,
             s.schedule.active_from, s.schedule.active_to)
    sched.start()
