"""Formatowanie wiadomości na kanał – dokładnie wg ustalonego szablonu."""
from __future__ import annotations

from ..models import Signal, SignalStatus


def _p(x: float) -> str:
    return f"{x:.2f}" if x >= 1 else f"{x:.3f}"


def format_signal(s: Signal, include_thesis: bool = True, disclaimer: str = "") -> str:
    lv = s.levels
    lines = [
        f"Nazwa spółki: {s.company_name} ({s.ticker})",
        f"Wejście: {_p(lv.entry_min)}-{_p(lv.entry_max)}",
    ]
    for i, t in enumerate(lv.targets, start=1):
        lines.append(f"Target {i}: {_p(t)}, +{lv.pct(t):.1f}%")
    lines.append(f"StopLoss: {_p(lv.stop_loss)}")
    if include_thesis and s.assessment.thesis:
        lines += ["", f"Katalizator: {s.assessment.thesis}"]
    if disclaimer:
        lines += ["", disclaimer.strip()]
    return "\n".join(lines)


_OUTCOME_TEXT = {
    SignalStatus.T1: "✅ Target 1 osiągnięty",
    SignalStatus.T2: "✅✅ Target 2 osiągnięty",
    SignalStatus.T3: "✅✅✅ Target 3 osiągnięty",
    SignalStatus.STOPPED: "❌ StopLoss",
    SignalStatus.EXPIRED: "⌛ Sygnał wygasł",
}


def format_outcome(s: Signal, status: SignalStatus) -> str:
    return f"{_OUTCOME_TEXT[status]}: {s.company_name} ({s.ticker})"
