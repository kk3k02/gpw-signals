"""Harmonogram: polling newsów w godzinach aktywności w dni sesyjne + tracker po sesji."""
from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from .market.calendar import is_session_day
from .pipeline import App

log = logging.getLogger(__name__)


def in_active_window(now: datetime, start: str, end: str) -> bool:
    return time.fromisoformat(start) <= now.time() <= time.fromisoformat(end)


def run_forever(app: App) -> None:
    s = app.settings
    tz = ZoneInfo(s.timezone)
    sched = BlockingScheduler(timezone=tz)

    def poll() -> None:
        now = datetime.now(tz)
        if not is_session_day(now.date(), s.schedule.holidays) or not in_active_window(
                now, s.schedule.active_from, s.schedule.active_to):
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

    def prices() -> None:
        if is_session_day(datetime.now(tz).date(), s.schedule.holidays):
            try:
                app.sync_prices()
            except Exception:
                log.exception("Błąd importu notowań")

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
    log.info("Start schedulera (co %d min, %s–%s)", s.schedule.poll_minutes,
             s.schedule.active_from, s.schedule.active_to)
    sched.start()
