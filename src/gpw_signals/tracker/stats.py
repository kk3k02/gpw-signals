"""Statystyki skuteczności – do kalibracji progów i parametrów poziomów."""
from __future__ import annotations

import json
from collections import Counter, defaultdict


def summarize(rows) -> dict:
    total = Counter()
    by_catalyst: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        st = r["status"]
        cat = json.loads(r["assessment"]).get("catalyst_type", "inne")
        total[st] += 1
        by_catalyst[cat][st] += 1

    def rates(c: Counter) -> dict:
        n = sum(c.values())
        hit1 = c["t1"] + c["t2"] + c["t3"]
        return {"n": n, "t1+": hit1, "t2+": c["t2"] + c["t3"], "t3": c["t3"], "sl": c["stopped"],
                "expired": c["expired"], "active": c["active"],
                "hit_rate_t1": round(hit1 / n, 3) if n else None}

    return {"all": rates(total), "by_catalyst": {k: rates(v) for k, v in by_catalyst.items()}}
