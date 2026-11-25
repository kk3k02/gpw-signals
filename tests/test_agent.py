"""Test pętli agenta na sztucznym kliencie (bez wywołań API)."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

from gpw_signals.agent.assess import NewsAssessor
from gpw_signals.agent.tools import ToolContext
from gpw_signals.config import AgentConfig
from gpw_signals.db import Database
from gpw_signals.ingest.macro import EventCalendar
from gpw_signals.mapping import TickerMap
from gpw_signals.models import NewsItem, SourceKind
from tests.conftest import make_ohlcv


class FakePrices:
    def daily(self, symbol, days=250):
        return make_ohlcv().tail(days)


class FakeMessages:
    def __init__(self, usage=None):
        self.calls, self.usage = [], usage

    def create(self, **params):
        self.calls.append(params)
        if len(self.calls) == 1:
            return NS(stop_reason="tool_use", usage=self.usage, model="claude-opus-5-5", content=[
                NS(type="tool_use", id="tu1", name="get_indicators", input={"ticker": "PKN"})])
        answer = {"actionable": True, "direction": "long", "catalyst_type": "umowa", "strength": 0.75,
                  "horizon_days": 8, "already_priced_in": False, "thesis": "Istotna umowa.", "risks": []}
        return NS(stop_reason="end_turn", usage=self.usage, model="claude-opus-5-5", content=[
            NS(type="text", text=json.dumps(answer))])


def run_agent(usage=None, on_usage=None):
    ctx = ToolContext(FakePrices(), TickerMap.from_csv("data/gpw_tickers.csv"), Database(":memory:"),
                      EventCalendar("brak.yaml"))
    fake = FakeMessages(usage)
    client = NS(messages=fake, beta=NS(messages=fake))
    assessor = NewsAssessor(AgentConfig(), ctx, client=client)
    assessor.on_usage = on_usage
    news = NewsItem("t", SourceKind.ESPI, "ORLEN umowa", "u", datetime.now(UTC), "treść")

    return assessor.assess(news, "PKN", "ORLEN"), fake


def test_agent_loop_with_tool_call():
    a, fake = run_agent()

    assert a.actionable and a.strength == 0.75 and a.catalyst_type == "umowa"
    first, second = fake.calls
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert first["output_config"]["format"]["type"] == "json_schema"
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "tu1" and not tool_result["is_error"]
    assert "atr" in json.loads(tool_result["content"])
