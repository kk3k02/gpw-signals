"""Wybór dostawcy oceny newsów wg `agent.provider`."""
from __future__ import annotations

from ..config import AgentConfig
from .assess import NewsAssessor
from .base import Assessor
from .ollama import OllamaAssessor
from .tools import ToolContext


def make_assessor(cfg: AgentConfig, ctx: ToolContext) -> Assessor:
    return OllamaAssessor(cfg, ctx) if cfg.provider == "ollama" else NewsAssessor(cfg, ctx)
