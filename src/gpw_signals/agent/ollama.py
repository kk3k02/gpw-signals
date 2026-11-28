"""Ocena newsa lokalnym modelem (Ollama): jedno zapytanie, dane wklejone do promptu zamiast narzędzi."""
from __future__ import annotations

import json

import httpx

from ..config import AgentConfig
from ..models import Assessment, NewsItem
from .assess import ASSESSMENT_SCHEMA, to_assessment
from .base import AgentError, AgentUnavailable, UsageCallback
from .prompts import CONTEXT_TEMPLATE, LOCAL_SYSTEM_PROMPT, USER_TEMPLATE, published_label
from .tools import ToolContext
from .usage import LOCAL_PREFIX, TokenUsage

_HISTORY_DAYS = 15
_RELATED_NEWS = 5
_SUMMARY_CHARS = 300   # wcześniejsze newsy
_NEWS_CHARS = 6000     # oceniany news: dłuższy wypchnąłby instrukcję z okna kontekstu
_HORIZON = (1, 30)


def validate(raw: str) -> dict:
    """Ściśle sprawdzona odpowiedź: lokalne modele mylą się częściej, więc nic nie poprawiamy po cichu."""
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AgentError(f"Niepoprawny JSON z modelu: {e}") from e
    props = ASSESSMENT_SCHEMA["properties"]
    if not isinstance(d, dict) or set(d) != set(props):
        raise AgentError("Odpowiedź modelu ma inne pola niż schemat")

    def check(ok: bool, name: str) -> None:
        if not ok:
            raise AgentError(f"Pole {name} poza zakresem: {d[name]!r}"[:300])

    def number(x) -> bool:
        return isinstance(x, int | float) and not isinstance(x, bool)

    check(isinstance(d["actionable"], bool), "actionable")
    check(isinstance(d["already_priced_in"], bool), "already_priced_in")
    check(d["direction"] in props["direction"]["enum"], "direction")
    check(d["catalyst_type"] in props["catalyst_type"]["enum"], "catalyst_type")
    check(number(d["strength"]) and 0 <= d["strength"] <= 1, "strength")
    check(number(d["horizon_days"]) and _HORIZON[0] <= d["horizon_days"] <= _HORIZON[1]
          and d["horizon_days"] == int(d["horizon_days"]), "horizon_days")
    check(isinstance(d["thesis"], str) and bool(d["thesis"].strip()), "thesis")
    check(isinstance(d["risks"], list) and all(isinstance(r, str) for r in d["risks"]), "risks")
    return d


class OllamaAssessor:
    def __init__(self, cfg: AgentConfig, ctx: ToolContext, client: httpx.Client | None = None):
        self.cfg, self.ctx = cfg, ctx
        self.client = client or httpx.Client(timeout=cfg.timeout_s)
        self.on_usage: UsageCallback | None = None

    def _context(self, news: NewsItem, ticker: str) -> str:
        def tool(name: str, **args) -> str:
            return self.ctx.run(name, {"ticker": ticker, **args})[0]

        # Oceniany news jest już w bazie – bez odfiltrowania wyglądałby na wcześniej znany
        related = [{**r, "summary": (r["summary"] or "")[:_SUMMARY_CHARS]}
                   for r in self.ctx.db.related_news(ticker) if r["title"] != news.title]
        return CONTEXT_TEMPLATE.format(
            info=tool("get_company_info"), indicators=tool("get_indicators"),
            history=tool("get_price_history", days=_HISTORY_DAYS),
            related=json.dumps(related[:_RELATED_NEWS], ensure_ascii=False),
            events=tool("get_upcoming_events"))

    def _chat(self, user: str) -> dict:
        try:
            r = self.client.post(f"{self.cfg.base_url.rstrip('/')}/api/chat", json={
                "model": self.cfg.model, "stream": False, "format": ASSESSMENT_SCHEMA,
                "think": False,  # qwen3 i inne modele "myślące": bez rozumowania, sam JSON
                "messages": [{"role": "system", "content": LOCAL_SYSTEM_PROMPT}, {"role": "user", "content": user}],
                "options": {"temperature": 0, "num_ctx": self.cfg.num_ctx,
                            "num_predict": min(self.cfg.max_tokens, self.cfg.num_ctx // 2)}})
        except httpx.TransportError as e:
            raise AgentUnavailable(f"Ollama {self.cfg.base_url}: {type(e).__name__}: {e}") from e
        # 404 = model niepobrany; to usterka środowiska, a nie newsa, więc też do ponowienia
        if r.status_code in (404, 408, 429) or r.status_code >= 500:
            raise AgentUnavailable(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200:
            raise AgentError(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
        try:
            body = r.json()
        except ValueError as e:
            raise AgentUnavailable(f"Ollama: odpowiedź nie jest JSON-em ({self.cfg.base_url})") from e
        if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
            raise AgentError("Ollama: nieoczekiwany format odpowiedzi")
        return body

    def assess(self, news: NewsItem, ticker: str, company: str) -> Assessment:
        user = USER_TEMPLATE.format(
            source=news.source, kind=news.kind.value, published_at=published_label(news.published_at), title=news.title,
            summary=news.summary[:_NEWS_CHARS] or "(brak)", url=news.url, company=company, ticker=ticker)
        resp = self._chat(f"{self._context(news, ticker)}\n\n{user}")
        if self.on_usage:
            self.on_usage(LOCAL_PREFIX + self.cfg.model,
                          TokenUsage(resp.get("prompt_eval_count") or 0, resp.get("eval_count") or 0))
        if resp.get("done_reason") == "length":
            raise AgentError("Odpowiedź modelu ucięta (limit num_predict/num_ctx)")
        return to_assessment(validate(resp["message"].get("content") or ""), ticker)
