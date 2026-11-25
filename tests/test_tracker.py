"""Tracker (#13): wynik w R i %, break-even, zamknięcie po celu, jedna wiadomość dziennie, podsumowanie."""
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from gpw_signals.config import Settings
from gpw_signals.db import Database
from gpw_signals.models import Assessment, Levels, Signal, SignalStatus
from gpw_signals.pipeline import App
from gpw_signals.publish.formatter import format_outcome
from gpw_signals.tracker.outcomes import CloseReason, evaluate
from gpw_signals.tracker.stats import summarize

NOW = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
ENTRY = (60, 61, 60, 60.5)
T1 = (61, 62.5, 60.5, 62)
STOP = (59, 59, 57, 58)


def _signal(horizon=10):
    # wejście 60 (środek strefy), ryzyko 2, cele +1R/+2R/+3R
    a = Assessment("PKN", True, "long", "umowa", 0.7, horizon, False, "x")
    return Signal("PKN", "ORLEN", Levels(59.5, 60.5, [62.0, 64.0, 66.0], 58.0), 0.7, a, "u1",
                  datetime(2026, 11, 2, 10, 0, tzinfo=UTC), telegram_message_id=42)


def _bars(*rows):
    idx = pd.bdate_range("2026-11-03", periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)


def test_stop_is_minus_one_r():
    out = evaluate(_signal(), _bars(ENTRY, (59, 59.5, 57.5, 58)))
    assert (out.reason, out.result_r, out.result_pct) == (CloseReason.STOP, -1.0, -3.33)


def test_gap_below_stop_exits_at_open():
    assert evaluate(_signal(), _bars(ENTRY, (56, 57, 55, 56))).result_r == -2.0


def test_all_targets():
    out = evaluate(_signal(), _bars(ENTRY, (62, 67, 61, 66)))
    assert (out.status, out.reason, out.result_r) == (SignalStatus.T3, CloseReason.TARGETS, 2.0)


def test_stop_after_t1():
    out = evaluate(_signal(), _bars(ENTRY, T1, STOP))
    assert (out.status, out.closed, out.reason) == (SignalStatus.T1, True, CloseReason.STOP_AFTER_TARGET)
    assert out.result_r == pytest.approx(-1 / 3, abs=1e-3)


def test_breakeven_only_when_enabled_and_from_next_session():
    bars = _bars(ENTRY, (60.5, 62.5, 59.8, 62), (61, 61, 59.9, 60))
    assert not evaluate(_signal(), bars).closed
    assert not evaluate(_signal(), bars.head(2), breakeven_after_t1=True).closed
    out = evaluate(_signal(), bars, breakeven_after_t1=True)
    assert (out.status, out.reason) == (SignalStatus.T1, CloseReason.BREAKEVEN)
    assert out.result_r == pytest.approx(1 / 3, abs=1e-3)


def test_horizon_uses_close_and_ignores_later_sessions():
    out = evaluate(_signal(horizon=2), _bars(ENTRY, (60, 61.5, 59, 61), (62, 67, 61, 66)))
    assert (out.status, out.reason, out.result_r) == (SignalStatus.EXPIRED, CloseReason.HORIZON, 0.5)


def test_not_entered_has_no_result():
    out = evaluate(_signal(horizon=1), _bars((70, 71, 69, 70)))
    assert (out.reason, out.result_r) == (CloseReason.NOT_ENTERED, None)


def test_outcome_messages():
    s = _signal()
    out = evaluate(s, _bars(ENTRY, T1, STOP))
    assert format_outcome(s, out) == "🔚 StopLoss po osiągnięciu Target 1: ORLEN (PKN)\nWynik: -0.33R, -1.1%"
    assert "Wynik" not in format_outcome(s, evaluate(s, _bars(ENTRY, T1)))


class _Pub:
    def __init__(self):
        self.sent, self.fail = [], False

    def send(self, text, reply_to=None):
        if self.fail:
            raise RuntimeError("telegram")
        self.sent.append((text, reply_to))


@pytest.fixture
def app():
    db = Database(":memory:")
    prices = SimpleNamespace(bars=None)
    prices.daily = lambda symbol, n: prices.bars
    tickers = SimpleNamespace(get=lambda t: SimpleNamespace(symbol="pkn"))
    a = App(Settings(), db, tickers, prices, None, [], _Pub(), clock=lambda: NOW)
    db.insert_signal(_signal())
    return a


def test_one_message_for_several_levels_and_one_per_day(app):
    t2 = (61, 64.5, 60.5, 64)
    app.prices.bars = _bars(ENTRY, t2)
    app.track()
    app.prices.bars = _bars(ENTRY, t2, STOP)
    app.track()
    assert app.publisher.sent == [("✅✅ Target 2 osiągnięty: ORLEN (PKN)", 42)]
    app.clock = lambda: NOW + timedelta(days=1)
    app.track()
    app.track()
    assert len(app.publisher.sent) == 2 and "StopLoss po osiągnięciu Target 2" in app.publisher.sent[1][0]
    assert app.db.open_signals() == []


def test_failed_send_is_retried(app):
    app.prices.bars = _bars(ENTRY, T1)
    app.publisher.fail = True
    app.track()
    assert app.db.open_signals()[0].status == SignalStatus.ACTIVE
    app.publisher.fail = False
    app.track()
    assert len(app.publisher.sent) == 1 and app.db.open_signals()[0].status == SignalStatus.T1


def test_stats_and_weekly_summary(app):
    assert app.weekly_summary() is False
    app.prices.bars = _bars(ENTRY, T1, STOP)
    app.track()
    st = summarize(app.db.all_signals())
    assert st["all"]["n"] == 1 and st["all"]["sl_after_target"] == 1
    assert st["all"]["avg_r"] == st["by_catalyst"]["umowa"]["avg_r"] == -0.333
    assert app.weekly_summary() and "Zamknięte sygnały: 1" in app.publisher.sent[-1][0]
    app.clock = lambda: NOW + timedelta(days=8)
    assert app.weekly_summary() is False


def test_migrates_signals_without_result_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    db = Database(path)
    db.insert_signal(_signal())
    db.conn.close()
    con = sqlite3.connect(path)
    for c in ("close_reason", "result_r", "result_pct", "last_outcome_on"):
        con.execute(f"ALTER TABLE signals DROP COLUMN {c}")
    con.commit()
    con.close()
    assert summarize(Database(path).all_signals())["all"]["n_result"] == 0
