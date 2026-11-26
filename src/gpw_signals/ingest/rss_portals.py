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


_GPW_SUFFIX = re.compile(r"\s*\((?:Komunikat|Uchwała)\)$")


def _clean(text: str) -> str:
    return html.unescape(_TAG.sub(" ", text or "")).strip()


_TOC = re.compile(r"^\s*Spis treści", re.IGNORECASE)


def _published(entry) -> datetime:
    """Data publikacji zawsze w UTC: feedparser zamienia strefę z pubDate (np. +0100) na UTC w *_parsed."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=UTC) if t else datetime.now(UTC)


def needs_body(item: NewsItem) -> bool:
    """RSS niesie tylko tytuł: brak opisu, opis = tytuł albo sam spis treści raportu (bankier.pl)."""
    s = item.summary.strip()
    return not s or s == item.title.strip() or bool(_TOC.match(s))


def parse_feed(raw: str | bytes, cfg: FeedConfig, keywords: list[str] | None = None) -> list[NewsItem]:
    feed = feedparser.parse(raw)
    out: list[NewsItem] = []
    for e in feed.entries:
        title, summary = _clean(e.get("title", "")), _clean(e.get("summary", ""))
        # gpw.pl/rss_komunikaty: "SPÓŁKA S.A. (Komunikat)" – sufiks nic nie wnosi do mapowania
        title = _GPW_SUFFIX.sub("", title)
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
