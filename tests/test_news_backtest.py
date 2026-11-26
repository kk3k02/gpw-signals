"""Backtest pipeline'u na historycznych newsach (#12): bez wycieku przyszłości i z raportem."""
from datetime import UTC, date, datetime

import pandas as pd

from gpw_signals.config import FiltersConfig, LevelsConfig, Settings
from gpw_signals.mapping import TickerMap
from gpw_signals.models import Assessment, NewsItem, SourceKind
from gpw_signals.news_backtest import AsOfPrices, backtest_news, load_news
from tests.conftest import make_ohlcv

END = "2026-03-31"


class Prices:
    def __init__(self):
        self.df = make_ohlcv(400, end=END)

    def daily(self, symbol, days=250):
        return self.df.tail(days)


class SpyAssessor:
    """Mock LLM: zapamiętuje ostatnią świecę widoczną przez narzędzia w chwili oceny."""

    def __init__(self, prices, catalyst="umowa"):
        self.prices, self.catalyst, self.seen = prices, catalyst, []

    def assess(self, news, ticker, company):
        self.seen.append((news.published_at, self.prices.daily("x").index[-1]))
        return Assessment(ticker, True, "long", self.catalyst, 0.8, 10, False, "test")


def _settings():
    return Settings(levels=LevelsConfig(resistance_snap_r=0.0),
                    filters=FiltersConfig(min_avg_turnover_pln=0, min_rr_t1=0.0, require_above_sma50=False,
                                          block_bear_regime=False, min_volume_ratio=0, max_gap_pct=100,
                                          min_score=0.0, cooldown_days=0))


def _news(ts, title="ORLEN zawarł istotną umowę", kind=SourceKind.ESPI, source="espi"):
    return NewsItem(source, kind, title, f"https://ex/{ts}", ts, "Treść.", 3)


def test_asof_hides_news_day_during_session():
    p = AsOfPrices(Prices())
    p.as_of = datetime(2026, 2, 11, 10, 0, tzinfo=UTC)   # 11:00 PL, w trakcie sesji
    assert p.daily("x").index[-1] == pd.Timestamp("2026-02-10")
    p.as_of = datetime(2026, 2, 11, 17, 0, tzinfo=UTC)   # 18:00 PL, po sesji
    assert p.daily("x").index[-1] == pd.Timestamp("2026-02-11")


def test_pipeline_runs_without_future_leak_and_reports():
    tickers = TickerMap.from_csv("data/gpw_tickers.csv")
    holder = {}

    def make(ctx):
        holder["a"] = SpyAssessor(ctx.prices)
        return holder["a"]

    news = [_news(datetime(2026, 1, 20, 9, 0, tzinfo=UTC)),
            _news(datetime(2026, 2, 11, 10, 0, tzinfo=UTC), "PKO BP podał wyniki", SourceKind.PORTAL, "bankier"),
            _news(datetime(2025, 12, 1, 9, 0, tzinfo=UTC)),                       # przed --from
            _news(datetime(2026, 1, 21, 9, 0, tzinfo=UTC), "Pogoda ładna", SourceKind.PORTAL)]
    rep = backtest_news(_settings(), tickers, Prices(), None, news, start=date(2026, 1, 1), make_assessor=make)
    assert rep.news == 3 and rep.decisions["brak tickera"] == 1
    for published, last_bar in holder["a"].seen:
        assert last_bar.date() < published.date()        # agent nie widzi sesji dnia newsa ani dalszych
    assert len(rep.signals) == 2, rep.details
    s = rep.summary()
    assert s["all"]["n"] == 2 and s["funnel"]["sygnał"] == 2
    assert set(s["by_catalyst"]) == {"umowa"}
    assert set(s["by_source"]) == {"espi:espi", "portal:bankier"}
    for sig in rep.signals:                                # horyzont 10 sesji mieści się w danych
        assert sig.outcome.closed


def test_signal_at_end_of_data_stays_open():
    tickers = TickerMap.from_csv("data/gpw_tickers.csv")
    p = Prices()
    rep = backtest_news(_settings(), tickers, p, SpyAssessor(p), [_news(datetime(2026, 3, 30, 9, 0, tzinfo=UTC))])
    [sig] = rep.signals
    assert not sig.outcome.closed and rep.summary()["all"]["active"] == 1


def test_load_news_naive_time_is_warsaw(tmp_path):
    f = tmp_path / "n.yaml"
    f.write_text("- {published_at: '2026-01-15T08:31:00', kind: espi, title: X}\n", encoding="utf-8")
    [n] = load_news(f)
    assert n.published_at == datetime(2026, 1, 15, 7, 31, tzinfo=UTC) and n.source == "espi"


def test_example_dataset_parses():
    assert len(load_news("data/backtest/news.example.yaml")) == 2
