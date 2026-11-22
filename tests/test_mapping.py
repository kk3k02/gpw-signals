"""Mapowanie news -> spółka (#8): emitent ESPI, dopasowanie w treści, pełna lista spółek."""
from pathlib import Path

import pandas as pd
import pytest
import yaml

from gpw_signals.mapping import TickerMap, abbreviates, espi_issuer, issuer_key
from gpw_signals.models import SourceKind
from gpw_signals.universe import update_universe

CASES = yaml.safe_load(Path("tests/fixtures/mapping_cases.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tm():
    return TickerMap.from_csv("data/gpw_tickers.csv")


def test_quality_on_labelled_titles(tm):
    """Precyzja ważniejsza od czułości: fałszywe trafienie to sygnał na złej spółce."""
    tp = fp = fn = 0
    wrong = []
    for c in CASES:
        got = set(tm.map_news(c["title"], c.get("summary", ""), SourceKind(c["kind"])))
        exp = set(c["expected"])
        tp, fp, fn = tp + len(got & exp), fp + len(got - exp), fn + len(exp - got)
        if got - exp:
            wrong.append((c["title"], sorted(got - exp)))
    assert fp == 0, wrong
    assert tp / (tp + fn) >= 0.95, f"czułość {tp / (tp + fn):.2f}"
    assert len(CASES) >= 80


def test_full_list_is_loaded(tm):
    assert len(tm.companies) > 300
    assert tm.get("PKN").short_name == "PKNORLEN" and tm.get("POLIMEXMS").isin == "PLMSTSD00019"


@pytest.mark.parametrize(("name", "key"), [
    ("ROBYG S.A.", "ROBYG"), ("Merlin Group SA", "MERLINGROUP"), ("ORLEN Spółka Akcyjna", "ORLEN"),
    ("POLIMEX-MOSTOSTAL", "POLIMEXMOSTOSTAL"), ("ZE PAK S.A.", "ZEPAK"), ("KGHM Polska Miedź S.A.", "KGHMPOLSKAMIEDZ"),
])
def test_issuer_key(name, key):
    assert issuer_key(name) == key


def test_abbreviates():
    assert abbreviates("POLIMEXMS", "POLIMEXMOSTOSTAL") and abbreviates("AIRWAY", "AIRWAYMEDIX")
    assert not abbreviates("POLICE", "POLIMEXMOSTOSTAL") and not abbreviates("PZU", "PZUSA")  # min. 4 znaki


def test_espi_maps_only_issuer_not_counterparties(tm):
    title = "KGHM Polska Miedź S.A.: Zawarcie umowy z ORLEN S.A. i PKO Bank Polski"
    assert tm.map_news(title, "Treść wymienia też Budimex.", SourceKind.ESPI) == ["KGH"]
    assert espi_issuer("Bez dwukropka") is None
    # ESPI bez formatu "EMITENT:" – wracamy do dopasowania w treści
    assert tm.map_news("Komunikat KGHM o umowie", "", SourceKind.ESPI) == ["KGH"]


def test_strict_tokens_only_uppercase(tm):
    assert tm.find("Wyniki LPP lepsze od oczekiwań") == ["LPP"]
    assert tm.find("Spółka lpp rośnie") == []  # skrót tylko WIELKIMI literami
    assert "GPW" not in tm.find("Rekordowe obroty na GPW")  # GPW jako rynek, nie spółka


# ------------------------------------------------------------------ generator listy

def _archive(rows):
    return pd.DataFrame(rows, columns=["isin", "name"])


def test_update_universe_keeps_curated_and_adds_missing():
    existing = [{"ticker": "PKN", "name": "ORLEN", "symbol": "pkn", "isin": "PLPKN0000018", "short_name": "",
                 "sector": "paliwa", "aliases": "PKN ORLEN"},
                {"ticker": "OLD", "name": "Stara", "symbol": "old", "isin": "PLOLD0000000", "short_name": "OLD",
                 "sector": "", "aliases": ""}]
    archive = _archive([("PLPKN0000018", "PKNORLEN"), ("PLMSTSD00019", "POLIMEXMS"), ("PLXX00000001", "PKN")])
    upd = update_universe(existing, archive)
    pkn = next(r for r in upd.rows if r["ticker"] == "PKN")
    assert pkn["short_name"] == "PKNORLEN" and pkn["aliases"] == "PKN ORLEN"  # opis ręczny bez zmian
    assert upd.added == ["POLIMEXMS"] and upd.not_in_archive == ["OLD"]
    assert upd.skipped_conflicts == ["PKN (PLXX00000001)"]  # ticker zajęty przez inną spółkę
    new = next(r for r in upd.rows if r["ticker"] == "POLIMEXMS")
    assert (new["name"], new["symbol"], new["short_name"]) == ("POLIMEXMS", "polimexms", "POLIMEXMS")
    assert update_universe(upd.rows, archive).added == []  # drugie uruchomienie niczego nie dodaje
