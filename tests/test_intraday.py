"""#4: bieżący kurs intraday, filtr luki od wczorajszego zamknięcia, pora newsa względem sesji."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from gpw_signals.agent.prompts import published_label
from gpw_signals.market.indicators import snapshot
from gpw_signals.market.intraday import (
    CsvIntradayProvider,
    NoIntraday,
    Quote,
    SessionPhase,
    fresh_quote,
    make_intraday,
    session_phase,
)

PL = ZoneInfo("Europe/Warsaw")


def _daily(last_day: str, n: int = 60) -> pd.DataFrame:
    idx = pd.bdate_range(end=last_day, periods=n)
    close = np.linspace(90, 100, n)
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close,
                         "volume": 1000.0}, index=idx)


def test_snapshot_without_live_uses_daily():
    s = snapshot(_daily("2026-11-24"))
    assert s.price_source == "daily" and s.close == 100


def test_live_price_change_from_yesterday_close():
    df = _daily("2026-11-24")  # ostatnia świeca = wczoraj
    s = snapshot(df, live_price=110.0, session_date=date(2026, 11, 25))
    assert s.price_source == "intraday" and s.close == 110 and s.prev_close == 100
    assert round(s.change_pct, 6) == 10.0


def test_live_price_skips_incomplete_today_bar():
    df = _daily("2026-11-25")
    df.iloc[-1, df.columns.get_loc("close")] = 101.0  # niepełna dzisiejsza świeca
    s = snapshot(df, live_price=110.0, session_date=date(2026, 11, 25))
    assert s.prev_close == float(df["close"].iloc[-2])


def test_session_phase():
    assert session_phase(datetime(2026, 11, 25, 7, 30, tzinfo=PL), PL) == SessionPhase.PRE
    assert session_phase(datetime(2026, 11, 25, 9, 0, tzinfo=PL), PL) == SessionPhase.IN
    assert session_phase(datetime(2026, 11, 25, 17, 30, tzinfo=PL), PL) == SessionPhase.AFTER
    assert "przed sesją" in published_label(datetime(2026, 11, 25, 7, 30, tzinfo=PL))


class _Fixed:
    def __init__(self, q):
        self.q = q

    def last_quote(self, symbol):
        return self.q


def test_fresh_quote_rules():
    now = datetime(2026, 11, 25, 12, 0, tzinfo=PL)
    ok = Quote(50.0, datetime(2026, 11, 25, 11, 50, tzinfo=PL))
    assert fresh_quote(_Fixed(ok), "x", now, PL, 20) == ok
    assert fresh_quote(_Fixed(Quote(50.0, datetime(2026, 11, 25, 11, 0, tzinfo=PL))), "x", now, PL, 20) is None
    assert fresh_quote(_Fixed(Quote(50.0, datetime(2026, 11, 24, 16, 0, tzinfo=PL))), "x", now, PL, 9999) is None
    assert fresh_quote(NoIntraday(), "x", now, PL, 20) is None


def test_csv_intraday_provider(tmp_path):
    (tmp_path / "abc.csv").write_text("datetime,open,high,low,close\n2026-11-25 09:05,1,1,1,10.5\n"
                                      "2026-11-25 09:10,1,1,1,10.7\n", encoding="utf-8")
    q = CsvIntradayProvider(tmp_path).last_quote("abc")
    assert q.price == 10.7 and q.at == datetime(2026, 11, 25, 9, 10, tzinfo=PL)
    assert CsvIntradayProvider(tmp_path).last_quote("brak") is None
    assert isinstance(make_intraday("none", ""), NoIntraday)
