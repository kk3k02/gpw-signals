"""Lokalny model przez Ollama (#23) na fałszywym serwerze HTTP."""
import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from gpw_signals.agent.assess import ASSESSMENT_SCHEMA, NewsAssessor
from gpw_signals.agent.base import AgentError, AgentUnavailable
from gpw_signals.agent.evaluate import load_cases, run_cases
from gpw_signals.agent.factory import make_assessor
from gpw_signals.agent.ollama import OllamaAssessor, validate
from gpw_signals.agent.prompts import LOCAL_SYSTEM_PROMPT, SYSTEM_PROMPT
from gpw_signals.agent.tools import ToolContext
from gpw_signals.agent.usage import TokenUsage
from gpw_signals.config import AgentConfig
from gpw_signals.db import Database
from gpw_signals.ingest.macro import EventCalendar
from gpw_signals.mapping import TickerMap
from gpw_signals.models import NewsItem, SourceKind
from tests.conftest import make_ohlcv
from tests.test_pipeline import FakeAssessor, _news, make_app  # noqa: F401 – fixture

CFG = AgentConfig(provider="ollama", model="bielik")
GOOD = {"actionable": True, "direction": "long", "catalyst_type": "umowa", "strength": 0.7, "horizon_days": 8,
        "already_priced_in": False, "thesis": "Istotna umowa.", "risks": ["ryzyko"]}
NEWS = NewsItem("t", SourceKind.ESPI, "ORLEN S.A.: Umowa", "u", datetime.now(UTC), "treść", tickers=["PKN"])


class Prices:
    def daily(self, symbol, days=250):
        return make_ohlcv().tail(days)


def _ctx(db=None):
    return ToolContext(Prices(), TickerMap.from_csv("data/gpw_tickers.csv"), db or Database(":memory:"),
                       EventCalendar("brak.yaml"))


def _reply(content=GOOD, **extra):
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}, "done_reason": "stop",
                                     "prompt_eval_count": 1200, "eval_count": 90, **extra})


def _assessor(handler, db=None):
    return OllamaAssessor(CFG, _ctx(db), client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_single_request_with_context_and_schema():
    reqs, usage = [], []
    db = Database(":memory:")
    db.insert_news_if_new(NEWS)
    db.insert_news_if_new(NewsItem("t", SourceKind.ESPI, "Starszy news", "u2", datetime.now(UTC), "", tickers=["PKN"]))

    def handler(req):
        reqs.append(json.loads(req.content))
        assert req.url == "http://localhost:11434/api/chat"
        return _reply()

    a = _assessor(handler, db)
    a.on_usage = lambda m, u: usage.append((m, u))
    res = a.assess(NEWS, "PKN", "ORLEN")
    assert res.actionable and res.strength == 0.7 and res.ticker == "PKN"
    (body,) = reqs
    assert body["format"] == ASSESSMENT_SCHEMA and body["stream"] is False and "tools" not in body
    user = body["messages"][1]["content"]
    assert '"atr"' in user and "Starszy news" in user
    assert user.count(NEWS.title) == 1  # oceniany news nie jest swoim własnym "wcześniejszym newsem"
    assert usage == [("ollama/bielik", TokenUsage(1200, 90))]
    assert usage[0][1].cost_usd("ollama/bielik") == 0.0


def test_prompts_differ_only_in_tools_paragraph():
    assert "Masz narzędzia" in SYSTEM_PROMPT and "Masz narzędzia" not in LOCAL_SYSTEM_PROMPT
    assert SYSTEM_PROMPT.split("Typologia")[1] == LOCAL_SYSTEM_PROMPT.split("Typologia")[1]


@pytest.mark.parametrize("change", [
    {"strength": 1.5}, {"strength": True}, {"strength": "0.7"}, {"horizon_days": 0}, {"horizon_days": 31},
    {"horizon_days": 2.5}, {"horizon_days": float("inf")}, {"horizon_days": float("nan")},
    {"direction": "short"}, {"catalyst_type": "plotka"}, {"actionable": "tak"},
    {"already_priced_in": 0}, {"thesis": " "}, {"risks": "x"}, {"extra": 1}])
def test_out_of_range_answer_is_agent_error(change):
    with pytest.raises(AgentError):
        validate(json.dumps({**GOOD, **change}))


@pytest.mark.parametrize("raw", ["", "nie json", "[]", "{}",
                                 json.dumps({**GOOD, "strength": float("nan")})])
def test_broken_answer_is_agent_error(raw):
    with pytest.raises(AgentError):
        _assessor(lambda r: _reply(raw)).assess(NEWS, "PKN", "ORLEN")


def test_truncated_answer_is_agent_error():
    with pytest.raises(AgentError, match="ucięta"):
        _assessor(lambda r: _reply(done_reason="length")).assess(NEWS, "PKN", "ORLEN")


def test_bad_request_is_agent_error():
    with pytest.raises(AgentError, match="400"):
        _assessor(lambda r: httpx.Response(400, text="bad")).assess(NEWS, "PKN", "ORLEN")


@pytest.mark.parametrize("status", [404, 429, 500, 503])
def test_server_problem_is_unavailable(status):
    with pytest.raises(AgentUnavailable):
        _assessor(lambda r: httpx.Response(status, text="x")).assess(NEWS, "PKN", "ORLEN")


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
def test_no_connection_is_unavailable(exc):
    def handler(req):
        raise exc("brak")
    with pytest.raises(AgentUnavailable):
        _assessor(handler).assess(NEWS, "PKN", "ORLEN")


def test_unavailable_model_keeps_news_in_queue(make_app):  # noqa: F811
    n = _news()
    app = make_app([n], assessor=FakeAssessor(AgentUnavailable("brak serwera")))
    assert app.run_once() == [] and app.db.news_row(n.uid)["processed"] == 0
    assert len(app.run_once()) == 1


def test_provider_selection():
    assert isinstance(make_assessor(CFG, _ctx()), OllamaAssessor)
    assert isinstance(make_assessor(AgentConfig(), _ctx()), NewsAssessor)


def test_ollama_needs_local_model_name():
    with pytest.raises(ValidationError, match=r"agent\.model"):
        AgentConfig(provider="ollama")
    with pytest.raises(ValidationError):
        AgentConfig(provider="inny")


def test_eval_report():
    cases = load_cases("data/eval/agent_cases.yaml")
    tm = TickerMap.from_csv("data/gpw_tickers.csv")
    assert all(tm.get(c["ticker"]) for c in cases)
    rep = run_cases(cases, CFG, tm, lambda cfg, ctx: _assessor(lambda r: _reply()))
    negatives = sum(not c["signal"] for c in cases)
    assert (rep.total, rep.errors, rep.false_negatives) == (len(cases), 0, 0)
    assert rep.false_positives == negatives > 0  # model "zawsze kupuj" musi wypaść źle
    broken = run_cases(cases, CFG, tm, lambda cfg, ctx: _assessor(lambda r: _reply("x")))
    assert broken.errors == len(cases)
