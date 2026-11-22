"""Strefy czasowe (#11): granice dnia w czasie PL, zmiana czasu, migracja dat, tracker na przełomie dnia."""
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from gpw_signals.clock import MARKET_TZ, from_iso, market_date, market_day_bounds, to_iso
from gpw_signals.db import Database
from gpw_signals.models import Assessment, Levels, Signal, SignalStatus
from gpw_signals.tracker.outcomes import evaluate


def test_day_bounds_winter():
    start, end = market_day_bounds(date(2026, 11, 23))
    assert start == datetime(2026, 11, 22, 23, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 23, 23, 0, tzinfo=UTC)


def test_day_bounds_dst_changes():
    # 29.03.2026: zmiana na czas letni (doba 23 h), 25.10.2026: na zimowy (doba 25 h)
    s, e = market_day_bounds(date(2026, 3, 29))
    assert e - s == timedelta(hours=23) and s == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    s, e = market_day_bounds(date(2026, 10, 25))
    assert e - s == timedelta(hours=25) and e == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)


def test_market_date_after_midnight_pl():
    # 23:30 UTC to już 00:30 następnego dnia w Warszawie
    assert market_date(datetime(2026, 11, 22, 23, 30, tzinfo=UTC)) == date(2026, 11, 23)
    assert market_date(datetime(2026, 7, 1, 21, 59, tzinfo=UTC)) == date(2026, 7, 1)
    assert market_date(datetime(2026, 7, 1, 22, 0, tzinfo=UTC)) == date(2026, 7, 2)  # lato: UTC+2


def test_iso_is_fixed_format_and_sortable():
    a = to_iso(datetime(2026, 11, 22, 10, 0, tzinfo=UTC))
    b = to_iso(datetime(2026, 11, 22, 11, 0, 0, 500, tzinfo=MARKET_TZ))  # 10:00:00.0005 UTC
    assert a == "2026-11-22T10:00:00.000000+00:00"
    assert a < b and from_iso(b) == datetime(2026, 11, 22, 10, 0, 0, 500, tzinfo=UTC)


def _signal(created_at: datetime) -> Signal:
    a = Assessment("PKN", True, "long", "umowa", 0.7, 10, False, "t")
    return Signal("PKN", "ORLEN", Levels(60.0, 61.0, [63.0, 65.0, 67.0], 58.0), 0.7, a, "u", created_at)


def test_count_signals_by_market_day():
    db = Database(":memory:")
    db.insert_signal(_signal(datetime(2026, 11, 22, 22, 30, tzinfo=UTC)))  # 23:30 PL, 22.11
    db.insert_signal(_signal(datetime(2026, 11, 22, 23, 30, tzinfo=UTC)))  # 00:30 PL, 23.11
    assert db.count_signals_between(*market_day_bounds(date(2026, 11, 22))) == 1
    assert db.count_signals_between(*market_day_bounds(date(2026, 11, 23))) == 1


def test_cooldown_uses_aware_since():
    db = Database(":memory:")
    s = _signal(datetime(2026, 11, 10, 12, 0, tzinfo=UTC))
    db.insert_signal(s)
    db.update_signal_status(s.id, SignalStatus.T1, closed_at=datetime(2026, 11, 12, tzinfo=UTC))
    assert db.has_recent_signal("PKN", datetime(2026, 11, 10, 11, 59, tzinfo=UTC))
    assert not db.has_recent_signal("PKN", datetime(2026, 11, 10, 12, 1, tzinfo=UTC))


def test_tracker_uses_market_session_date():
    # Sygnał z 00:30 PL 3.11 (23:30 UTC 2.11): świeca z 3.11 to dzień publikacji, nie sesja PO sygnale
    s = _signal(datetime(2026, 11, 2, 23, 30, tzinfo=UTC))
    bars = pd.DataFrame({"high": [61.0], "low": [57.0]}, index=pd.DatetimeIndex(["2026-11-03"]))
    out = evaluate(s, bars)
    assert out.status == SignalStatus.ACTIVE and not out.closed


def test_migrates_naive_local_dates(tmp_path):
    path = tmp_path / "legacy.sqlite"
    Database(path).conn.close()  # schemat bieżący
    naive = "2026-11-20T10:15:00"  # stary format: datetime.now() serwera, bez strefy
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 0")
    con.execute("INSERT INTO signals (ticker, created_at, closed_at, status, assessment) VALUES (?,?,?,?,?)",
                ("PKN", naive, None, "active", "{}"))
    con.execute("INSERT INTO news (uid, published_at, processed) VALUES ('n1', '2026-11-20T09:00:00+01:00', 1)")
    con.commit()
    con.close()

    db = Database(path)
    created = db.conn.execute("SELECT created_at FROM signals").fetchone()[0]
    published = db.conn.execute("SELECT published_at FROM news").fetchone()[0]
    assert from_iso(created) == datetime.fromisoformat(naive).astimezone()  # czas lokalny serwera -> UTC
    assert published == "2026-11-20T08:00:00.000000+00:00"
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1

    Database(path)  # drugi start: bez ponownej konwersji
    again = sqlite3.connect(path).execute("SELECT created_at FROM signals").fetchone()[0]
    assert again == created
