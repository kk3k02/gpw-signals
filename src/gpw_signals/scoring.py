"""Filtry rynkowe + łączny score. Twarde filtry odrzucają, miękkie przemnażają siłę katalizatora.

Podział na dwa etapy (#5), żeby nie płacić za ocenę LLM newsów, które i tak odpadną:
- `pre_agent_check` – wszystko, co zależy tylko od rynku i poziomów; wołane PRZED agentem,
- `score_signal`     – filtry zależne od oceny agenta i końcowy score; wołane PO agencie.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import FiltersConfig
from .market.indicators import Snapshot
from .market.regime import Regime
from .models import Assessment, Levels, SourceKind

# Waga wiarygodności źródła: komunikat spółki > rekomendacja DM > portal > makro
SOURCE_WEIGHT = {SourceKind.ESPI: 1.0, SourceKind.BROKER: 0.9, SourceKind.PORTAL: 0.75, SourceKind.MACRO: 0.6}
REGIME_WEIGHT = {Regime.BULL: 1.05, Regime.NEUTRAL: 1.0, Regime.BEAR: 0.85}
MAX_STRENGTH = 1.0


@dataclass
class ScoreResult:
    score: float
    passed: bool
    rejections: list[str] = field(default_factory=list)
    factors: dict[str, float] = field(default_factory=dict)


def _above_sma50(snap: Snapshot) -> bool:
    return not math.isnan(snap.sma50) and snap.close > snap.sma50


def market_rejections(snap: Snapshot, cfg: FiltersConfig) -> list[str]:
    """Twarde filtry niezależne od oceny agenta."""
    rej: list[str] = []
    if snap.avg_turnover_20 < cfg.min_avg_turnover_pln:
        rej.append(f"płynność {snap.avg_turnover_20:,.0f} zł < {cfg.min_avg_turnover_pln:,.0f} zł")
    if snap.change_pct > cfg.max_gap_pct:
        rej.append(f"kurs już +{snap.change_pct:.1f}% – nie gonimy")
    return rej


def market_factors(snap: Snapshot, lv: Levels, regime: Regime, kind: SourceKind,
                   cfg: FiltersConfig) -> dict[str, float]:
    """Miękkie czynniki score'u poza siłą katalizatora (tej nie znamy przed agentem)."""
    rr_t1 = (lv.targets[0] - lv.entry_mid) / lv.risk if lv.risk > 0 else 0
    sma200 = snap.sma200 if not math.isnan(snap.sma200) else 0
    return {
        "source": SOURCE_WEIGHT.get(kind, 0.6),
        "volume": 1.0 if snap.volume_ratio >= cfg.min_volume_ratio else 0.85,
        "trend": 1.0 if _above_sma50(snap) and snap.sma50 > sma200 else 0.9,
        "regime": REGIME_WEIGHT[regime],
        "rr": min(1.1, 0.9 + 0.1 * rr_t1),
    }


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def pre_agent_check(snap: Snapshot, lv: Levels, regime: Regime, kind: SourceKind,
                    cfg: FiltersConfig) -> list[str]:
    """Powody odrzucenia, które da się stwierdzić bez agenta. Pusta lista = warto zapytać agenta.

    Poza twardymi filtrami liczy górną granicę score'u (siła katalizatora = 1.0). Jeśli nawet
    najsilniejszy katalizator nie dobiłby do `min_score`, ocena LLM niczego nie zmieni.
    """
    rej = market_rejections(snap, cfg)
    if rej:
        return rej
    best = _clamp(MAX_STRENGTH * math.prod(market_factors(snap, lv, regime, kind, cfg).values()))
    if best < cfg.min_score:
        rej.append(f"maks. możliwy score {best:.2f} < {cfg.min_score}")
    return rej


def agent_rejections(a: Assessment, snap: Snapshot, regime: Regime, cfg: FiltersConfig,
                     earnings_soon: bool) -> list[str]:
    """Twarde filtry zależne od oceny agenta (kierunek, dyskonto, wyjątki dla silnych katalizatorów)."""
    rej: list[str] = []
    if not a.actionable or a.direction != "long":
        rej.append("agent: brak sygnału long")
    if a.already_priced_in:
        rej.append("agent: news zdyskontowany")
    if cfg.block_bear_regime and regime == Regime.BEAR and a.strength < cfg.strong_catalyst_override:
        rej.append("rynek w bessie")
    if cfg.require_above_sma50 and not _above_sma50(snap) and a.strength < cfg.strong_catalyst_override:
        rej.append("kurs poniżej SMA50")
    if earnings_soon and a.catalyst_type != "wyniki":
        rej.append("wyniki spółki w ciągu 3 dni")
    return rej


def score_signal(a: Assessment, snap: Snapshot, lv: Levels, regime: Regime, kind: SourceKind,
                 cfg: FiltersConfig, earnings_soon: bool = False) -> ScoreResult:
    """Pełna ocena (wszystkie filtry + score). Zachowuje to samo zachowanie co przed podziałem."""
    rej = agent_rejections(a, snap, regime, cfg, earnings_soon) + market_rejections(snap, cfg)
    factors = {"strength": a.strength, **market_factors(snap, lv, regime, kind, cfg)}
    score = _clamp(math.prod(factors.values()))
    passed = not rej and score >= cfg.min_score
    if not rej and not passed:
        rej.append(f"score {score:.2f} < {cfg.min_score}")
    return ScoreResult(score, passed, rej, factors)
