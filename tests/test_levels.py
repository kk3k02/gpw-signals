from gpw_signals.levels.engine import compute_levels
from gpw_signals.levels.validate import validate_levels
from gpw_signals.market.indicators import Snapshot, snapshot
from gpw_signals.market.tick_size import round_down, round_up, tick_for
from gpw_signals.models import Levels


def _snap(**kw) -> Snapshot:
    base = dict(close=100.0, prev_close=99.0, atr=2.0, sma20=98, sma50=95, sma200=90,
                avg_turnover_20=5e6, volume_ratio=1.5, change_pct=1.0, high_52w=120, low_52w=80,
                resistances=[], supports=[])
    base.update(kw)
    return Snapshot(**base)


def test_levels_basic_ordering(lcfg):
    lr = compute_levels(_snap(), lcfg)
    lv = lr.levels
    assert lv is not None, lr.reason
    assert lv.stop_loss < lv.entry_min <= lv.entry_max < lv.targets[0] < lv.targets[1] < lv.targets[2]
    assert validate_levels(lv) == []
    # entry 99.4-100.4 (tick 0.05 >= 100), SL = 99.4 - 3.0 = 96.4
    assert lv.entry_min == 99.40 and lv.entry_max == 100.40
    assert lv.stop_loss == 96.40


def test_target_snaps_below_resistance(lcfg):
    raw = compute_levels(_snap(), lcfg).levels
    res = raw.targets[1] - 0.5  # opór tuż pod T2
    snapped = compute_levels(_snap(resistances=[res]), lcfg).levels
    assert snapped.targets[1] < res
    assert snapped.targets[1] < raw.targets[1]


def test_sl_too_far_rejected(lcfg):
    lr = compute_levels(_snap(atr=10.0), lcfg)  # SL ~ -18%
    assert lr.levels is None and "SL za daleko" in lr.reason


def test_sl_uses_swing_low(lcfg):
    lv = compute_levels(_snap(supports=[96.0]), lcfg).levels
    assert lv.stop_loss <= 96.0 - 0.2 * 2.0


def test_snapshot_from_ohlcv(ohlcv, lcfg):
    s = snapshot(ohlcv)
    assert s.atr > 0 and s.resistances and s.supports
    assert compute_levels(s, lcfg).levels is not None


def test_validate_rejects_bad_order():
    errs = validate_levels(Levels(10, 11, [10.5, 12, 13], 9))
    assert errs and "kolejność" in errs[0]


def test_validate_rr():
    errs = validate_levels(Levels(10, 10, [10.5, 11, 12], 9), min_rr_t1=1.0)
    assert any("R:R" in e for e in errs)


def test_tick_rounding():
    assert tick_for(0.5) == 0.001 and tick_for(75) == 0.02
    assert round_down(75.031) == 75.02 and round_up(75.001) == 75.02
    assert round_down(3.0) == 3.0
