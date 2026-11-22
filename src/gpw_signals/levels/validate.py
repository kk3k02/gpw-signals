"""Twarde reguły spójności poziomów – ostatnia linia obrony przed opublikowaniem bzdury."""
from __future__ import annotations

from ..models import Levels


def validate_levels(lv: Levels, min_rr_t1: float = 1.0) -> list[str]:
    errors: list[str] = []
    if len(lv.targets) != 3:
        errors.append("Wymagane dokładnie 3 cele")
        return errors
    t1, t2, t3 = lv.targets
    if not (0 < lv.stop_loss < lv.entry_min <= lv.entry_max < t1 < t2 < t3):
        errors.append(f"Zła kolejność: SL={lv.stop_loss} wejście={lv.entry_min}-{lv.entry_max} T={lv.targets}")
        return errors
    rr = (t1 - lv.entry_mid) / lv.risk if lv.risk > 0 else 0
    if rr < min_rr_t1 - 1e-9:
        errors.append(f"R:R do T1 = {rr:.2f} < {min_rr_t1}")
    return errors
