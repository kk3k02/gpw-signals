from datetime import date

from gpw_signals.market.calendar import easter, gpw_holidays, is_session_day
from gpw_signals.market.gpw_archive import session_days


def test_easter_known_years():
    assert easter(2026) == date(2026, 4, 5)
    assert easter(2027) == date(2027, 3, 28)


def test_gpw_holidays_2026():
    expected = {"2026-01-01", "2026-01-06", "2026-04-03", "2026-04-06", "2026-05-01", "2026-06-04",
                "2026-11-11", "2026-12-24", "2026-12-25", "2026-12-31"}
    assert {d.isoformat() for d in gpw_holidays(2026)} == expected


def test_gpw_holidays_2027():
    expected = {"2027-01-01", "2027-01-06", "2027-03-26", "2027-03-29", "2027-05-03", "2027-05-27",
                "2027-11-01", "2027-11-11", "2027-12-24", "2027-12-31"}
    assert {d.isoformat() for d in gpw_holidays(2027)} == expected


def test_scheduler_skips_holiday_and_extra_closure():
    assert not is_session_day(date(2026, 12, 24))      # Wigilia (czwartek)
    assert not is_session_day(date(2026, 12, 26))      # sobota
    assert is_session_day(date(2026, 12, 28))
    assert not is_session_day(date(2026, 12, 28), ["2026-12-28"])  # zamknięcie ad hoc z konfiguracji


def test_session_days_use_builtin_calendar():
    assert session_days(date(2026, 12, 23), date(2026, 12, 28)) == [date(2026, 12, 23), date(2026, 12, 28)]


def test_earnings_within_on_real_calendar_file():
    from pathlib import Path

    from gpw_signals.ingest.macro import EventCalendar

    cal = EventCalendar(Path(__file__).resolve().parents[1] / "data" / "macro_calendar.yaml")
    assert cal.earnings_within("LPP", 3, today=date(2026, 12, 8))
    assert cal.earnings_within("lpp", 3, today=date(2026, 12, 10))
    assert not cal.earnings_within("LPP", 3, today=date(2026, 12, 6))   # 4 dni przed
    assert not cal.earnings_within("LPP", 3, today=date(2026, 12, 11))  # już po
    assert not cal.earnings_within("PKO", 3, today=date(2026, 12, 8))   # inna spółka
    assert not cal.earnings_within("LPP", 3, today=date(2026, 12, 1))   # tylko RPP w oknie
