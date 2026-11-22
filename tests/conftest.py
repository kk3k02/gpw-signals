import numpy as np
import pandas as pd
import pytest

from gpw_signals.config import FiltersConfig, LevelsConfig


def make_ohlcv(n: int = 260, start: float = 50.0, drift: float = 0.001, vol: float = 0.015,
               seed: int = 7, volume: float = 200_000, end: str | None = None) -> pd.DataFrame:
    """Syntetyczne świece kończące się na `end` (domyślnie dziś) – świeże dla kontroli jakości danych."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start * np.exp(np.cumsum(rets))
    open_ = np.r_[start, close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, vol, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, vol, n))
    idx = pd.bdate_range(end=pd.Timestamp(end) if end else pd.Timestamp.today().normalize(), periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, volume)}, index=idx)


@pytest.fixture
def ohlcv():
    return make_ohlcv()


@pytest.fixture
def lcfg():
    return LevelsConfig()


@pytest.fixture
def fcfg():
    return FiltersConfig()
