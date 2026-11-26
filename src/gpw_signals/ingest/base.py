"""Wspólny interfejs źródeł newsów."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from ..models import NewsItem

log = logging.getLogger(__name__)


class Source(Protocol):
    name: str

    def fetch(self) -> list[NewsItem]: ...


def fetch_all(sources: list[Source],
              on_result: Callable[[str, Exception | None], None] = lambda name, error: None) -> list[NewsItem]:
    """Pobiera ze wszystkich źródeł; awaria jednego źródła nie zatrzymuje pozostałych."""
    items: list[NewsItem] = []
    for s in sources:
        try:
            got = s.fetch()
            log.info("Źródło %s: %d pozycji", s.name, len(got))
            items.extend(got)
            on_result(s.name, None)
        except Exception as e:  # noqa: BLE001 – izolacja błędów źródeł
            log.warning("Źródło %s niedostępne: %s", s.name, e)
            on_result(s.name, e)
    # Najpierw ważniejsze źródła i najnowsze newsy
    return sorted(items, key=lambda n: (-n.priority, -n.published_at.timestamp()))
