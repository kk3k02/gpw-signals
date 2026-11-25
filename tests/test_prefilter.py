"""Pre-filtr szumu i liczniki kosztu (#6)."""
from datetime import UTC, date, datetime
from types import SimpleNamespace as NS

import pytest
import yaml

from gpw_signals.agent.usage import TokenUsage
from gpw_signals.config import PrefilterConfig
from gpw_signals.db import Database
from gpw_signals.models import NewsItem, SourceKind
from gpw_signals.prefilter import noise_reason
from tests.test_agent import run_agent
from tests.test_pipeline import FakeAssessor, _news, make_app  # noqa: F401 – fixture

CFG = PrefilterConfig()


def _reason(title, summary="", kind=SourceKind.ESPI, cfg=CFG):
    return noise_reason(NewsItem("t", kind, title, "u", datetime.now(UTC), summary), cfg)


@pytest.mark.parametrize("subject", [
    "Zwołanie Nadzwyczajnego Walnego Zgromadzenia", "Ogłoszenie o zwołaniu ZWZ na dzień 15 grudnia",
    "Projekty uchwał na NWZ", "Wykaz akcjonariuszy posiadających co najmniej 5% głosów na WZ",
    "Zmiana w składzie Rady Nadzorczej", "Rezygnacja członka Rady Nadzorczej",
    "Terminy publikacji raportów okresowych w 2027 roku", "Wybór firmy audytorskiej"])
def test_noise_is_dropped(subject):
    assert _reason(f"ORLEN S.A.: {subject}")


@pytest.mark.parametrize(("subject", "summary"), [
    ("Zwołanie NWZ", "Porządek obrad: uchwała o wypłacie dywidendy."),
    ("Zwołanie NWZ w sprawie połączenia spółek", ""), ("Projekty uchwał – skup akcji własnych", ""),
    ("Zmiana w składzie Zarządu", ""), ("Zawarcie znaczącej umowy", ""),
    ("Rezygnacja członka Rady Nadzorczej", "Osoba została powołana na Prezesa Zarządu."),
    ("Wyniki finansowe QSr 3/2026", ""), ("Raport bieżący", "")])
def test_material_news_passes(subject, summary):
    assert _reason(f"ORLEN S.A.: {subject}", summary) is None


def test_issuer_name_is_not_matched():
    assert _reason("Zwołanie WZA SA: Data premiery gry") is None


def test_only_espi_and_only_when_enabled():
    assert _reason("ORLEN: Zwołanie NWZ", kind=SourceKind.PORTAL) is None
    assert _reason("ORLEN: Zwołanie NWZ", cfg=PrefilterConfig(enabled=False)) is None


@pytest.mark.parametrize(("summary", "dropped"), [
    ("Łączna wartość transakcji: 45 tys. zł", True), ("Wartość transakcji 120 500,50 PLN", True),
    ("Łączna wartość: 3,5 mln zł", False), ("Nabycie 1 000 000 akcji po cenie 5 zł", False), ("", False)])
def test_insider_threshold(summary, dropped):
    title = "LPP S.A.: Powiadomienie o transakcjach, o którym mowa w art. 19 MAR"
    assert bool(_reason(title, summary)) is dropped


def test_real_titles_are_not_dropped():
    with open("tests/fixtures/mapping_cases.yaml", encoding="utf-8") as f:
        cases = yaml.safe_load(f)
    assert [c["title"] for c in cases if _reason(c["title"], c.get("summary", ""), SourceKind(c["kind"]))] == []


def test_pipeline_skips_llm_and_counts(make_app):  # noqa: F811
    noise, good = _news("ORLEN S.A.: Zwołanie NWZ"), _news("KGHM Polska Miedź S.A.: Zawarcie umowy")
    app = make_app([noise, good], assessor=FakeAssessor())
    app.run_once()
    assert app.assessor.calls == 1 and "szum" in app.db.news_row(noise.uid)["decision"]
    (day,) = app.db.daily_report()
    assert day["stages"]["bez_llm:szum"] == 1 and day["stages"]["llm"] == 1 and day["stages"]["sygnał"] == 1


def test_agent_reports_usage_of_every_round():
    got = []
    run_agent(usage=NS(input_tokens=100, output_tokens=10, cache_read_input_tokens=None),
              on_usage=lambda model, u: got.append((model, u)))
    assert got == [("claude-opus-5-5", TokenUsage(100, 10))] * 2


def test_cost():
    u = TokenUsage(1_000_000, 100_000, 1_000_000, 100_000)
    assert u.cost_usd("claude-opus-5-5") == pytest.approx(4 + 2 + 0.2 + 0.5)
    assert u.cost_usd("inny") is None


def test_daily_report_sums_usage():
    db, d = Database(":memory:"), date(2026, 11, 25)
    db.add_usage(d, "claude-opus-5-5", TokenUsage(1_000_000, 0))
    db.add_usage(d, "claude-opus-5-5", TokenUsage(0, 100_000))
    db.add_stage_counts(d, {"llm": 1})
    db.add_stage_counts(d, {"llm": 2})
    (r,) = db.daily_report()
    assert r["day"] == "2026-11-25" and r["stages"] == {"llm": 3}
    assert r["models"]["claude-opus-5-5"]["requests"] == 2 and r["cost_usd"] == pytest.approx(6.0)
    db.add_usage(d, "inny", TokenUsage(1, 1))
    assert db.daily_report()[0]["cost_usd"] is None
