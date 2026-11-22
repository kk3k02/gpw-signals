"""Wspólny interfejs źródeł newsów."""
from __future__ import annotations

import logging
from typing import Protocol

from ..models import NewsItem

log = logging.getLogger(__name__)


class Source(Protocol):
    name: str

    def fetch(self) -> list[NewsItem]: ...


def fetch_all(sources: list[Source]) -> list[NewsItem]:
    """Pobiera ze wszystkich źródeł; awaria jednego źródła nie zatrzymuje pozostałych."""
    items: list[NewsItem] = []
    for s in sources:
        try:
            got = s.fetch()
            log.info("Źródło %s: %d pozycji", s.name, len(got))
            items.extend(got)
        except Exception as e:  # noqa: BLE001 – izolacja błędów źródeł
            log.warning("Źródło %s niedostępne: %s", s.name, e)
    # Najpierw ważniejsze źródła i najnowsze newsy
    return sorted(items, key=lambda n: (-n.priority, -n.published_at.timestamp()))
