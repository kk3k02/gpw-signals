"""Formatowanie wiadomości na kanał – dokładnie wg ustalonego szablonu."""
from __future__ import annotations

from ..models import Signal, SignalStatus
from ..tracker.outcomes import CloseReason, Outcome


def _p(x: float) -> str:
    return f"{x:.2f}" if x >= 1 else f"{x:.3f}"


def format_signal(s: Signal, include_thesis: bool = True, disclaimer: str = "",
                  methodology_url: str = "", conflict_note: str = "") -> str:
    """`conflict_note` – ujawnienie konfliktu interesów (pusty = brak konfliktu dla tej spółki)."""
    lv = s.levels
    lines = [
        f"Nazwa spółki: {_label(s)}",
        f"Wejście: {_p(lv.entry_min)}-{_p(lv.entry_max)}",
    ]
    for i, t in enumerate(lv.targets, start=1):
        lines.append(f"Target {i}: {_p(t)}, +{lv.pct(t):.1f}%")
    lines.append(f"StopLoss: {_p(lv.stop_loss)}")
    if include_thesis and s.assessment.thesis:
        lines += ["", f"Katalizator: {s.assessment.thesis}"]
    if conflict_note:
        lines += ["", conflict_note.strip()]
    if disclaimer:
        lines += ["", disclaimer.strip()]
    if methodology_url:
        lines.append(f"Metodologia: {methodology_url.strip()}")
    return "\n".join(lines)


def conflict_note_for(ticker: str, conflict_tickers: list[str], note: str) -> str:
    """Dopisek o konflikcie interesów, gdy spółka jest na liście pozycji autora."""
    return note if ticker.upper() in {t.strip().upper() for t in conflict_tickers} else ""


_OUTCOME_TEXT = {
    SignalStatus.T1: "✅ Target 1 osiągnięty",
    SignalStatus.T2: "✅✅ Target 2 osiągnięty",
    SignalStatus.T3: "✅✅✅ Target 3 osiągnięty",
    SignalStatus.STOPPED: "❌ StopLoss",
    SignalStatus.EXPIRED: "⌛ Sygnał wygasł",
}


# Zamknięcie po osiągnięciu celu – osobny komunikat, bo status zostaje T1/T2
_CLOSE_TEXT = {
    CloseReason.STOP_AFTER_TARGET: "🔚 StopLoss po osiągnięciu Target {n}",
    CloseReason.BREAKEVEN: "🔚 StopLoss na poziomie wejścia po osiągnięciu Target {n}",
    CloseReason.HORIZON: "🔚 Koniec horyzontu po osiągnięciu Target {n}",
}
_TARGET_NO = {SignalStatus.T1: 1, SignalStatus.T2: 2, SignalStatus.T3: 3}


def _label(s: Signal) -> str:
    # Spółki z listy generowanej mają nazwę równą tickerowi (nazwa skrócona GPW) – bez powtórzenia
    return s.company_name if s.company_name.upper() == s.ticker.upper() else f"{s.company_name} ({s.ticker})"


def format_outcome(s: Signal, out: Outcome) -> str:
    """Jedna wiadomość o stanie sygnału: najwyższy cel, ewentualne zamknięcie i wynik."""
    n = _TARGET_NO.get(out.status)
    if out.reason in _CLOSE_TEXT and n:
        head = _CLOSE_TEXT[out.reason].format(n=n)
    elif out.reason == CloseReason.NOT_ENTERED:
        head = "⌛ Sygnał wygasł bez wejścia"
    else:
        head = _OUTCOME_TEXT[out.status]
    lines = [f"{head}: {_label(s)}"]
    if out.result_r is not None and out.result_pct is not None:
        lines.append(f"Wynik: {out.result_r:+.2f}R, {out.result_pct:+.1f}%")
    return "\n".join(lines)


def format_weekly_summary(stats: dict) -> str:
    lines = ["📊 Podsumowanie tygodnia",
             f"Zamknięte sygnały: {stats['n']}",
             f"Target 1+: {stats['t1+']}, Target 2+: {stats['t2+']}, Target 3: {stats['t3']}",
             f"StopLoss: {stats['sl']}, StopLoss po celu: {stats['sl_after_target']}, wygasłe: {stats['expired']}"]
    if stats["n_result"]:
        lines.append(f"Średni wynik: {stats['avg_r']:+.2f}R, {stats['avg_pct']:+.1f}% "
                     f"(pozycji: {stats['n_result']})")
    return "\n".join(lines)
