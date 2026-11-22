from datetime import datetime

import pandas as pd

from gpw_signals.models import Assessment, Levels, Signal, SignalStatus
from gpw_signals.publish.formatter import format_signal
from gpw_signals.tracker.outcomes import evaluate


def _signal(horizon=10) -> Signal:
    a = Assessment("PKN", True, "long", "umowa", 0.7, horizon, False, "Duży kontrakt.")
    return Signal("PKN", "ORLEN", Levels(60.0, 61.0, [63.0, 65.0, 67.0], 58.0), 0.7, a, "u1",
                  datetime(2026, 11, 2, 10, 0))


def test_format_matches_template():
    txt = format_signal(_signal(), include_thesis=False)
    assert txt == (
        "Nazwa spółki: ORLEN (PKN)\n"
        "Wejście: 60.00-61.00\n"
        "Target 1: 63.00, +4.1%\n"
        "Target 2: 65.00, +7.4%\n"
        "Target 3: 67.00, +10.7%\n"
        "StopLoss: 58.00"
    )


def test_format_with_thesis_and_disclaimer():
    txt = format_signal(_signal(), include_thesis=True, disclaimer="Nie jest to rekomendacja.")
    assert "Katalizator: Duży kontrakt." in txt and txt.endswith("Nie jest to rekomendacja.")


def _bars(rows):
    idx = pd.bdate_range("2026-11-03", periods=len(rows))
    return pd.DataFrame(rows, columns=["high", "low"], index=idx)


def test_tracker_t2_then_open():
    out = evaluate(_signal(), _bars([(61.5, 60.5), (63.5, 61.0), (65.2, 62.0)]))
    assert out.status == SignalStatus.T2 and not out.closed


def test_tracker_stop_first():
    out = evaluate(_signal(), _bars([(61.0, 59.0), (60.0, 57.5)]))
    assert out.status == SignalStatus.STOPPED and out.closed


def test_tracker_same_bar_counts_stop():
    out = evaluate(_signal(), _bars([(64.0, 57.0)]))
    assert out.status == SignalStatus.STOPPED


def test_tracker_never_entered_expires():
    out = evaluate(_signal(horizon=2), _bars([(70, 62), (71, 63)]))
    assert out.status == SignalStatus.EXPIRED and out.closed


def test_tracker_t3_closes():
    out = evaluate(_signal(), _bars([(61, 60), (68, 61)]))
    assert out.status == SignalStatus.T3 and out.closed


def test_format_generated_company_without_duplicate_name():
    s = _signal()
    s.ticker = s.company_name = "POLIMEXMS"
    assert format_signal(s, include_thesis=False).startswith("Nazwa spółki: POLIMEXMS\n")
