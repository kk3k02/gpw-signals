"""Wspólny interfejs dostawców oceny newsów (Claude, lokalny model)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..models import Assessment, NewsItem
from .usage import TokenUsage

UsageCallback = Callable[[str, TokenUsage], None]


class AgentError(RuntimeError):
    """Ocena się nie udała i ponowienie nic nie da (odmowa, limity, niepoprawna odpowiedź)."""


class AgentUnavailable(RuntimeError):
    """Serwer modelu chwilowo niedostępny – news wraca do kolejki."""


class Assessor(Protocol):
    on_usage: UsageCallback | None

    def assess(self, news: NewsItem, ticker: str, company: str) -> Assessment: ...
