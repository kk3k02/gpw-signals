"""Kalendarz (#10): dni bez sesji GPW i terminy raportów okresowych z ESPI."""
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gpw_signals.config import Settings, load_settings
from gpw_signals.db import Database
from gpw_signals.ingest.macro import EventCalendar
from gpw_signals.ingest.report_schedule import (
    ReportCalendar,
    ScheduleError,
    extract_body,
    is_report_schedule,
    parse_dates,
)
from gpw_signals.mapping import TickerMap
from gpw_signals.models import NewsItem, SourceKind
from gpw_signals.pipeline import App
from gpw_signals.scheduler import missing_holiday_years, should_poll

PUB = date(2026, 1, 22)
PAGE = """<nav>Walne 5 maja 2026 r.</nav><b>Treść raportu:</b><p>Na podstawie § 83 rozporządzenia z dnia
6 czerwca 2025 r. Spółka podaje terminy:</p><li>raport roczny &ndash; 23&nbsp;marca 2026 r.</li>
<li>Q1 - 2026-05-27</li><li>Q3 - 16.11.2026</li>
<b>MESSAGE (ENGLISH VERSION)</b> 24.12.2026 <footer>1 lipca 2026 r.</footer>"""
CHANGE = ("raport roczny z terminu pierwotnie wyznaczonego na 20 kwietnia 2026 roku, na dzień 30 kwietnia 2026 roku; "
          "Q3 z terminu pierwotnie wyznaczonego na 23 listopada 2026 roku, na dzień 30 listopada 2026 roku.")


# ------------------------------------------------------------------ dni bez sesji

@pytest.fixture(scope="module")
def schedule():
    return load_settings("config/settings.yaml").schedule


def test_holidays_are_weekdays_for_both_years(schedule):
    days = [date.fromisoformat(d) for d in schedule.holidays]
    assert len(days) == len(set(days)) and all(d.weekday() < 5 for d in days)
    assert [sum(d.year == y for d in days) for y in (2026, 2027)] == [10, 10]


@pytest.mark.parametrize("day, expected", [
    ((2026, 11, 11), False), ((2026, 12, 24), False), ((2027, 3, 26), False), ((2027, 5, 27), False),
    ((2026, 11, 14), False),               # sobota
    ((2026, 11, 12), True), ((2027, 5, 28), True)])
def test_scheduler_skips_days_without_session(schedule, day, expected):
    assert should_poll(datetime(*day, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw")), schedule) is expected


def test_missing_holiday_years(schedule):
    assert missing_holiday_years(date(2026, 12, 1), schedule.holidays) == []
    assert missing_holiday_years(date(2027, 12, 1), schedule.holidays) == [2028]
    assert missing_holiday_years(date(2028, 1, 1), schedule.holidays) == [2028]


# ------------------------------------------------------------------ terminy raportów

@pytest.mark.parametrize("title, expected", [
    ("GPW SA (1/2026) Terminy przekazywania raportów okresowych w 2026 roku", True),
    ("GTC: Zmiana kalendarza raportów okresowych w 2026 roku", True),
    ("KGHM: Harmonogram publikacji raportów okresowych", True),
    ("ORLEN: Raport okresowy za III kwartał", False),
    ("ORLEN: Termin wypłaty dywidendy", False)])
def test_title(title, expected):
    assert is_report_schedule(title) is expected


def test_parses_only_polish_body():
    dates, replaced = parse_dates(extract_body(PAGE), PUB)
    assert dates == {date(2026, 3, 23), date(2026, 5, 27), date(2026, 11, 16)} and not replaced


def test_page_without_body_is_rejected(tmp_path):
    assert extract_body("<p>Menu 23 marca 2026 r.</p>") == ""
    with pytest.raises(ScheduleError):
        ReportCalendar(tmp_path / "r.yaml").import_announcement("GTC", "Terminy", "", PUB)
    assert not (tmp_path / "r.yaml").exists()


def test_change_replaces_only_listed_dates(tmp_path):
    p = tmp_path / "r.yaml"
    rc = ReportCalendar(p)
    rc.update("GTC", [date(2026, 4, 20), date(2026, 9, 21), date(2026, 11, 23)])
    rc.update("PKN", [date(2026, 11, 23)])
    rc.import_announcement("GTC", "Zmiana kalendarza raportów okresowych", CHANGE, date(2026, 4, 15))
    ev = EventCalendar(p).events
    assert sorted(d for d, items in ev.items() if any("GTC" in i for i in items)) == [
        date(2026, 4, 30), date(2026, 9, 21), date(2026, 11, 30)]
    assert ev[date(2026, 11, 23)] == ["ticker:PKN Wyniki okresowe"]


def test_full_schedule_replaces_future_dates(tmp_path):
    rc = ReportCalendar(tmp_path / "r.yaml")
    rc.update("GPW", [date(2026, 1, 10), date(2026, 3, 1)])
    rc.import_announcement("GPW", "Terminy przekazywania raportów okresowych", extract_body(PAGE), PUB)
    assert sorted(EventCalendar(rc.path).events) == [
        date(2026, 1, 10), date(2026, 3, 23), date(2026, 5, 27), date(2026, 11, 16)]


def test_calendar_merges_files(tmp_path):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text('"2026-12-10":\n  - "Dane GUS"\n', encoding="utf-8")
    cal = EventCalendar(a, extra=[b])
    ReportCalendar(b).update("LPP", [date(2026, 12, 10)])
    assert not cal.earnings_within("LPP", today=date(2026, 12, 8))
    cal.reload()
    assert cal.earnings_within("LPP", today=date(2026, 12, 8))
    assert len(cal.upcoming(14, "PKN", today=date(2026, 12, 8))) == 1


# ------------------------------------------------------------------ pipeline

class _Source:
    name = "fake"

    def __init__(self, items):
        self.items = items

    def fetch(self):
        out, self.items = self.items, []
        return out


class _NoPrices:
    def daily(self, symbol, n):
        raise RuntimeError("offline")


class _NoLlm:
    def assess(self, *a):
        raise AssertionError("komunikat z terminami nie powinien trafić do LLM")


def _app(tmp_path, enabled=True):
    now = datetime.now(UTC)
    news = NewsItem("f", SourceKind.ESPI, "ORLEN: Terminy przekazywania raportów okresowych", "https://ex.pl/1",
                    now - timedelta(minutes=5), "", 3)
    s = Settings()
    s.sources.fetch_report_schedules = enabled
    rp = tmp_path / "reports.yaml"
    s.sources.report_calendar_file = str(rp)
    app = App(s, Database(":memory:"), TickerMap.from_csv("data/gpw_tickers.csv"), _NoPrices(),
              EventCalendar(tmp_path / "none.yaml", extra=[rp]), [_Source([news])], None, assessor=_NoLlm())
    day = now.date() + timedelta(days=2)
    app.fetch_page = lambda url: f"Treść raportu: raport kwartalny {day.isoformat()} PODPISY OSÓB"
    return app, news


def test_pipeline_imports_schedule_without_llm(tmp_path):
    app, news = _app(tmp_path)
    app.run_once()
    row = app.db.news_row(news.uid)
    assert row["processed"] and "zapisano 1" in row["decision"]
    assert app.cycle_stats["news:terminy raportów"] == 1
    assert app.calendar.earnings_within("PKN")      # działa bez restartu


def test_pipeline_retries_fetch_error(tmp_path):
    app, news = _app(tmp_path)

    def boom(url):
        raise RuntimeError("503")
    app.fetch_page = boom
    app.run_once()
    row = app.db.news_row(news.uid)
    assert not row["processed"] and "503" in row["last_error"]


def test_pipeline_disabled_does_not_fetch(tmp_path):
    app, news = _app(tmp_path, enabled=False)
    app.run_once()
    assert not app.calendar.events
    assert "szum – terminy raportów" in app.db.news_row(news.uid)["decision"]  # odrzuca pre-filtr (#6)


# ------------------------------------------------------------------ pliki danych w repo

def test_holidays_come_from_data_file(schedule):
    from gpw_signals.config import load_holidays
    assert schedule.holidays_file == "data/gpw_holidays.yaml"
    assert sorted(load_holidays(schedule.holidays_file)) == schedule.holidays


def test_holidays_file_rejects_wrong_year(tmp_path):
    from gpw_signals.config import load_holidays
    p = tmp_path / "h.yaml"
    p.write_text('2026:\n  - "2027-01-01"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_holidays(p)


def test_poll_tick_skips_cycle_on_holiday(schedule):
    from gpw_signals.scheduler import poll_tick

    class _App:
        settings = Settings(schedule=schedule)
        settings.monitoring.heartbeat_file = "/nonexistent-dir/x/heartbeat"

        def run_once(self):
            raise AssertionError("cykl w dzień bez sesji")

        def clock(self):
            return datetime.now(UTC)
    poll_tick(_App(), datetime(2026, 12, 24, 10, 0, tzinfo=ZoneInfo("Europe/Warsaw")))


@pytest.mark.parametrize("today, expected", [
    (date(2026, 12, 7), True), (date(2026, 12, 10), True), (date(2026, 12, 6), False), (date(2026, 12, 11), False)])
def test_earnings_within_3_days_on_repo_calendar(today, expected):
    cal = EventCalendar(load_settings("config/settings.yaml").sources.macro_calendar_file)
    assert cal.earnings_within("LPP", today=today) is expected
    assert not cal.earnings_within("PKN", today=today)
