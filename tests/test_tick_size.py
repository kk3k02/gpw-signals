"""Krok notowań wg RTS 11 (#9): granice przedziałów, pasma płynności, dane ESMA."""
from datetime import date

import pytest

from gpw_signals.levels.engine import compute_levels
from gpw_signals.mapping import Company, TickerMap
from gpw_signals.market.indicators import snapshot
from gpw_signals.market.liquidity import current_bands, fetch_records
from gpw_signals.market.tick_size import (
    _PRICE_BOUNDS,
    _TABLE,
    BANDS,
    band_for_adnt,
    round_down,
    round_up,
    tick_for,
)


@pytest.mark.parametrize(("adnt", "band"), [
    (0, 1), (9.99, 1), (10, 2), (79.99, 2), (80, 3), (599.9, 3), (600, 4), (1999.9, 4), (2000, 5),
    (8999.9, 5), (9000, 6), (50000, 6)])
def test_band_boundaries(adnt, band):
    assert band_for_adnt(adnt) == band


@pytest.mark.parametrize(("price", "band", "tick"), [
    (0.0999, 1, 0.0005), (0.1, 1, 0.001), (0.9999, 4, 0.0005), (1, 4, 0.001), (19.99, 3, 0.02), (20, 3, 0.05),
    (99.99, 6, 0.01), (100, 6, 0.02), (499, 2, 1), (500, 2, 2), (49999, 1, 200), (50000, 1, 500)])
def test_tick_boundaries(price, band, tick):
    assert tick_for(price, band) == tick


def test_table_is_monotonic():
    """Krok rośnie z ceną i maleje z płynnością."""
    ticks = [t for _, t in _TABLE]
    assert all(len(t) == len(BANDS) and list(t) == sorted(t, reverse=True) for t in ticks)
    assert all(list(col) == sorted(col) for col in zip(*ticks, strict=True))


@pytest.mark.parametrize("band", BANDS)
@pytest.mark.parametrize("bound", _PRICE_BOUNDS[1:])
def test_rounding_at_price_boundaries(bound, band):
    assert round_down(bound, band) == round_up(bound, band) == bound
    below = bound * (1 - 1e-5)
    assert round_up(below, band) == bound
    assert round_down(below, band) == pytest.approx(bound - tick_for(below, band))


def test_rounding():
    assert round_down(75.031, 5) == 75.02 and round_up(75.001, 5) == 75.02
    assert round_down(75.13, 2) == 75.0 and round_up(75.13, 2) == 75.2


def test_unknown_band_rejected():
    with pytest.raises(ValueError, match="pasmo"):
        tick_for(10, 7)
    with pytest.raises(ValueError, match="pasmo"):
        Company("X", "X", "x", "", [], liquidity_band=0)


@pytest.mark.parametrize("band", BANDS)
def test_levels_lie_on_tick_grid(ohlcv, lcfg, band):
    lv = compute_levels(snapshot(ohlcv), lcfg, band).levels
    assert lv is not None
    for p in (lv.entry_min, lv.entry_max, lv.stop_loss, *lv.targets):
        assert round_down(p, band) == round_up(p, band) == p


def test_ticker_list_has_bands():
    tm = TickerMap.from_csv("data/gpw_tickers.csv")
    assert tm.get("PKN").liquidity_band == 6
    assert {c.liquidity_band for c in tm.companies.values()} == set(BANDS)


def _year(year, adnt, calc, **kw):
    return {"isin": "PL1", "methodology": "YEAR", "calculation_period_to": f"{year}-12-31T00:00:00Z",
            "calculation_time": calc, "mrmtl_adnte": adnt, "non_applicable_flag": "F", **kw}


def test_yearly_band_applies_from_april():
    recs = [_year(2025, 700, "2026-02-27T13:00:00Z"), _year(2024, 50, "2025-01-23T10:00:00Z")]
    assert current_bands(recs, date(2026, 3, 31))["PL1"] == (2, date(2025, 4, 1))
    assert current_bands(recs, date(2026, 4, 1))["PL1"] == (4, date(2026, 4, 1))


def test_correction_wins_and_unusable_records_are_skipped():
    recs = [_year(2025, 700, "2026-02-27T13:00:00Z"), _year(2025, 90, "2026-03-05T13:00:00Z"),
            _year(2025, 9999, "2026-03-09T13:00:00Z", non_applicable_flag="T"), {"isin": "PL2"}]
    assert current_bands(recs, date(2026, 5, 1)) == {"PL1": (3, date(2026, 4, 1))}


def test_new_listing_uses_estimate():
    rec = {"isin": "PL3", "methodology": "ESTM", "application_period_from": "2026-04-15T00:00:00Z",
           "calculation_time": "2026-05-14T06:00:00Z", "mrmtl_adnte": 30.5}
    assert current_bands([rec], date(2026, 5, 20))["PL3"][0] == 2


def test_fetch_batches_and_detects_truncation():
    calls = []

    def query(q):
        calls.append(q)
        return {"numFound": 1, "docs": [{"isin": "X"}]}

    assert len(fetch_records([f"PL{i:010d}" for i in range(51)], query)) == 2 and len(calls) == 2
    with pytest.raises(ValueError, match="niepełna"):
        fetch_records(["A"], lambda q: {"numFound": 5, "docs": []})
