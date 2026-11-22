"""Generyczny czytnik RSS/Atom. Obsługuje feedy ESPI/EBI, portali i rekomendacji (różnią się tylko `kind`)."""
from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import feedparser
import httpx

from ..config import FeedConfig
from ..models import NewsItem, SourceKind

_TAG = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return html.unescape(_TAG.sub(" ", text or "")).strip()


def _published(entry) -> datetime:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=UTC) if t else datetime.now(UTC)


def parse_feed(raw: str | bytes, cfg: FeedConfig, keywords: list[str] | None = None) -> list[NewsItem]:
    feed = feedparser.parse(raw)
    out: list[NewsItem] = []
    for e in feed.entries:
        title, summary = _clean(e.get("title", "")), _clean(e.get("summary", ""))
        kind = SourceKind(cfg.kind)
        # Na portalach wyłapujemy rekomendacje DM po słowach kluczowych
        if keywords and kind == SourceKind.PORTAL:
            text = f"{title} {summary}".lower()
            # Słowo kluczowe = rdzenie oddzielone spacją (odmiana: "cen docelow" łapie "cenę docelową")
            if any(all(stem in text for stem in k.lower().split()) for k in keywords):
                kind = SourceKind.BROKER
        out.append(NewsItem(source=cfg.name, kind=kind, title=title, url=e.get("link", ""),
                            published_at=_published(e), summary=summary[:2000], priority=cfg.priority))
    return out


class RssSource:
    def __init__(self, cfg: FeedConfig, keywords: list[str] | None = None):
        self.cfg, self.name, self.keywords = cfg, cfg.name, keywords

    def fetch(self) -> list[NewsItem]:
        r = httpx.get(self.cfg.url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "gpw-signals/0.1 (+rss reader)"})
        r.raise_for_status()
        return parse_feed(r.content, self.cfg, self.keywords)
