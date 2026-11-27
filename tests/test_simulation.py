import numpy as np
import pandas as pd
import pytest

from gpw_signals.models import Levels
from gpw_signals.simulation import simulate_target_first


def _df(drift: float, vol: float = 0.01, n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    close = 100 * np.cumprod(1 + drift + rng.normal(0, vol, n))
    return pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995,
                         "close": close, "volume": 1000},
                        index=pd.date_range("2025-01-01", periods=n))


LV = Levels(entry_min=99.5, entry_max=100.5, targets=[103, 106, 110], stop_loss=97)


def test_uptrend_gives_high_probability():
    r = simulate_target_first(_df(0.006), LV, 10, seed=1)
    assert r.n_paths == 10_000
    assert r.probability > 0.7
    assert r.probability + r.p_stop <= 1


def test_downtrend_gives_low_probability():
    r = simulate_target_first(_df(-0.004), LV, 10, seed=1)
    assert r.probability < 0.3


def test_same_day_hit_counts_as_stop():
    # Każda sesja ma zasięg ±5%: cel i SL padają tego samego dnia -> zawsze SL
    df = _df(0.0, vol=0.0)
    df["high"], df["low"] = df["close"] * 1.05, df["close"] * 0.95
    r = simulate_target_first(df, LV, 5, seed=1)
    assert r.probability == 0 and r.p_stop == 1


def test_short_history_raises():
    with pytest.raises(ValueError):
        simulate_target_first(_df(0.0, n=10), LV, 5)
