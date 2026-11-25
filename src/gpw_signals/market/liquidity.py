"""Pasma płynności spółek z rejestru ESMA FITRS (oficjalny ADNT z najważniejszego rynku, per ISIN)."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date

import httpx

from .tick_size import band_for_adnt

FITRS_URL = "https://registers.esma.europa.eu/solr/esma_registers_fitrs_equities/select"
_BATCH = 50
_ROWS = 2000


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def effective_from(rec: dict) -> date | None:
    """Od kiedy stosuje się ADNT (RTS 11 art. 3): roczny od 1 kwietnia, pozostałe od publikacji."""
    if rec.get("methodology") == "YEAR":
        end = _day(rec.get("calculation_period_to"))
        return date(end.year + 1, 4, 1) if end else None
    return _day(rec.get("application_period_from")) or _day(rec.get("calculation_time"))


def current_bands(records: Iterable[dict], today: date) -> dict[str, tuple[int, date]]:
    """ISIN -> (pasmo, data obowiązywania) z najnowszego rekordu obowiązującego w dniu `today`."""
    best: dict[str, tuple[tuple[date, str], dict]] = {}
    for r in records:
        start = effective_from(r)
        if start is None or start > today or r.get("mrmtl_adnte") is None or r.get("non_applicable_flag") == "T":
            continue
        key = (start, r.get("calculation_time", ""))  # korekta tego samego okresu: nowsze wyliczenie
        if r["isin"] not in best or key > best[r["isin"]][0]:
            best[r["isin"]] = (key, r)
    return {isin: (band_for_adnt(r["mrmtl_adnte"]), key[0]) for isin, (key, r) in best.items()}


def _http_query(query: str) -> dict:
    r = httpx.get(FITRS_URL, params={"q": query, "rows": _ROWS, "wt": "json"}, timeout=60)
    r.raise_for_status()
    return r.json()["response"]


def fetch_records(isins: Iterable[str], query: Callable[[str], dict] = _http_query) -> list[dict]:
    todo, out = sorted(set(isins)), []
    for i in range(0, len(todo), _BATCH):
        resp = query(f"isin:({' OR '.join(todo[i:i + _BATCH])})")
        if resp["numFound"] > len(resp["docs"]):
            raise ValueError(f"ESMA FITRS: niepełna odpowiedź ({len(resp['docs'])} z {resp['numFound']})")
        out += resp["docs"]
    return out
