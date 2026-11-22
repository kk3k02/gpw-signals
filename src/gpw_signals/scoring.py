"""Filtry rynkowe + łączny score. Twarde filtry odrzucają, miękkie przemnażają siłę katalizatora."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import FiltersConfig
from .market.indicators import Snapshot
from .market.regime import Regime
from .models import Assessment, Levels, SourceKind

# Waga wiarygodności źródła: komunikat spółki > rekomendacja DM > portal > makro
SOURCE_WEIGHT = {SourceKind.ESPI: 1.0, SourceKind.BROKER: 0.9, SourceKind.PORTAL: 0.75, SourceKind.MACRO: 0.6}


@dataclass
class ScoreResult:
    score: float
    passed: bool
    rejections: list[str] = field(default_factory=list)
    factors: dict[str, float] = field(default_factory=dict)


def score_signal(a: Assessment, snap: Snapshot, lv: Levels, regime: Regime, kind: SourceKind,
                 cfg: FiltersConfig, earnings_soon: bool = False) -> ScoreResult:
    rej: list[str] = []

    # --- twarde filtry ---
    if not a.actionable or a.direction != "long":
        rej.append("agent: brak sygnału long")
    if a.already_priced_in:
        rej.append("agent: news zdyskontowany")
    if snap.avg_turnover_20 < cfg.min_avg_turnover_pln:
        rej.append(f"płynność {snap.avg_turnover_20:,.0f} zł < {cfg.min_avg_turnover_pln:,.0f} zł")
    if snap.change_pct > cfg.max_gap_pct:
        rej.append(f"kurs już +{snap.change_pct:.1f}% – nie gonimy")
    if cfg.block_bear_regime and regime == Regime.BEAR and a.strength < cfg.strong_catalyst_override:
        rej.append("rynek w bessie")
    above_sma50 = not math.isnan(snap.sma50) and snap.close > snap.sma50
    if cfg.require_above_sma50 and not above_sma50 and a.strength < cfg.strong_catalyst_override:
        rej.append("kurs poniżej SMA50")
    if earnings_soon and a.catalyst_type != "wyniki":
        rej.append("wyniki spółki w ciągu 3 dni")

    # --- miękkie czynniki ---
    rr_t1 = (lv.targets[0] - lv.entry_mid) / lv.risk if lv.risk > 0 else 0
    factors = {
        "strength": a.strength,
        "source": SOURCE_WEIGHT.get(kind, 0.6),
        "volume": 1.0 if snap.volume_ratio >= cfg.min_volume_ratio else 0.85,
        "trend": 1.0 if above_sma50 and snap.sma50 > (snap.sma200 if not math.isnan(snap.sma200) else 0) else 0.9,
        "regime": {Regime.BULL: 1.05, Regime.NEUTRAL: 1.0, Regime.BEAR: 0.85}[regime],
        "rr": min(1.1, 0.9 + 0.1 * rr_t1),
    }
    score = math.prod(factors.values())
    score = max(0.0, min(1.0, score))
    passed = not rej and score >= cfg.min_score
    if not rej and not passed:
        rej.append(f"score {score:.2f} < {cfg.min_score}")
    return ScoreResult(score, passed, rej, factors)
