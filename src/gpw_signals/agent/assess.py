"""Agent oceniający news: pętla tool-use (Messages API) zakończona ustrukturyzowaną oceną JSON."""
from __future__ import annotations

import json
import logging

import anthropic

from ..config import AgentConfig
from ..models import Assessment, NewsItem
from .prompts import SYSTEM_PROMPT, USER_TEMPLATE
from .tools import TOOL_DEFINITIONS, ToolContext

log = logging.getLogger(__name__)

CATALYSTS = ["wyniki", "umowa", "dywidenda", "buyback", "wezwanie", "rekomendacja", "m&a", "regulacja",
             "makro", "inne"]

ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "actionable": {"type": "boolean"},
        "direction": {"type": "string", "enum": ["long", "none"]},
        "catalyst_type": {"type": "string", "enum": CATALYSTS},
        "strength": {"type": "number", "description": "0-1"},
        "horizon_days": {"type": "integer", "description": "Horyzont tezy w sesjach, 1-30"},
        "already_priced_in": {"type": "boolean"},
        "thesis": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["actionable", "direction", "catalyst_type", "strength", "horizon_days",
                 "already_priced_in", "thesis", "risks"],
    "additionalProperties": False,
}

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AgentError(RuntimeError):
    pass


class NewsAssessor:
    def __init__(self, cfg: AgentConfig, ctx: ToolContext, client: anthropic.Anthropic | None = None):
        self.cfg, self.ctx = cfg, ctx
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        # Leniwie – żeby komendy bez LLM (backtest, track) działały bez klucza API
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def _create(self, messages: list[dict]):
        params = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            # Stały system prompt + stałe narzędzia => prefiks cache'owalny między newsami
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=TOOL_DEFINITIONS,
            messages=messages,
            output_config={"effort": self.cfg.effort,
                           "format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA}},
        )
        if self.cfg.use_fallbacks:
            # Przy odmowie modelu serwer sam ponawia zapytanie na modelu zapasowym
            return self.client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
        return self.client.messages.create(**params)

    def assess(self, news: NewsItem, ticker: str, company: str) -> Assessment:
        messages: list[dict] = [{"role": "user", "content": USER_TEMPLATE.format(
            source=news.source, kind=news.kind.value, published_at=news.published_at.isoformat(),
            title=news.title, summary=news.summary or "(brak)", url=news.url, company=company, ticker=ticker)}]

        for _ in range(self.cfg.max_tool_rounds):
            resp = self._create(messages)
            log.debug("agent stop=%s usage=%s", resp.stop_reason, resp.usage)

            if resp.stop_reason == "refusal":
                raise AgentError(f"Model odmówił oceny ({getattr(resp, 'stop_details', None)})")
            if resp.stop_reason == "max_tokens":
                raise AgentError("Przekroczono max_tokens podczas oceny")
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason == "tool_use" and tool_uses:
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for tu in tool_uses:  # wszystkie wyniki w JEDNEJ wiadomości user
                    content, is_err = self.ctx.run(tu.name, tu.input)
                    results.append({"type": "tool_result", "tool_use_id": tu.id,
                                    "content": content, "is_error": is_err})
                messages.append({"role": "user", "content": results})
                continue

            text = next((b.text for b in resp.content if b.type == "text"), None)
            if not text:
                raise AgentError(f"Brak odpowiedzi tekstowej (stop={resp.stop_reason})")
            return _to_assessment(json.loads(text), ticker)

        raise AgentError("Agent przekroczył limit rund narzędzi")


def _to_assessment(d: dict, ticker: str) -> Assessment:
    return Assessment(
        ticker=ticker,
        actionable=bool(d["actionable"]),
        direction=d["direction"],
        catalyst_type=d["catalyst_type"],
        strength=max(0.0, min(1.0, float(d["strength"]))),
        horizon_days=max(1, min(30, int(d["horizon_days"]))),
        already_priced_in=bool(d["already_priced_in"]),
        thesis=d["thesis"].strip(),
        risks=list(d.get("risks", [])),
    )
