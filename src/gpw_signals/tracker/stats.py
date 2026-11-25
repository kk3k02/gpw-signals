"""Statystyki skuteczności – do kalibracji progów i parametrów poziomów."""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from .outcomes import CloseReason


def summarize(rows) -> dict:
    total = Counter()
    by_catalyst: dict[str, Counter] = defaultdict(Counter)
    results: dict[str | None, list[tuple[float, float]]] = defaultdict(list)  # None = wszystkie
    for r in rows:
        st = r["status"]
        cat = json.loads(r["assessment"]).get("catalyst_type", "inne")
        total[st] += 1
        by_catalyst[cat][st] += 1
        for key, counter in ((None, total), (cat, by_catalyst[cat])):
            if r["close_reason"] in (CloseReason.STOP_AFTER_TARGET, CloseReason.BREAKEVEN):
                counter["sl_after_target"] += 1
            if r["result_r"] is not None:
                results[key].append((r["result_r"], r["result_pct"]))

    def rates(c: Counter, res: list[tuple[float, float]]) -> dict:
        after = c.pop("sl_after_target", 0)  # nie jest statusem, nie wchodzi do n
        n = sum(c.values())
        k = len(res)
        hit1 = c["t1"] + c["t2"] + c["t3"]
        return {"n": n, "t1+": hit1, "t2+": c["t2"] + c["t3"], "t3": c["t3"], "sl": c["stopped"],
                "sl_after_target": after,
                "expired": c["expired"], "active": c["active"],
                "hit_rate_t1": round(hit1 / n, 3) if n else None,
                # Wynik tylko z zamkniętych sygnałów z wejściem w pozycję (starsze bez wyniku są pomijane)
                "n_result": k, "win_rate": round(sum(x > 0 for x, _ in res) / k, 3) if k else None,
                "avg_r": round(sum(x for x, _ in res) / k, 3) if k else None,
                "avg_pct": round(sum(x for _, x in res) / k, 2) if k else None}

    return {"all": rates(total, results[None]),
            "by_catalyst": {k: rates(v, results[k]) for k, v in by_catalyst.items()}}
