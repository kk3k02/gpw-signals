"""Raporty ESPI/EBI wszystkich spółek z money.pl/gielda/komunikaty (#7).

Strona nie ma RSS, ale osadza dane w JSON-ie stanu aplikacji:
- lista: `"stockStatementsData":{"data":[{id, stock, stockUrlRel, espiUrlRel, time, title}, ...]}`
  – 20 najnowszych komunikatów, bez stronicowania (przy odpytywaniu co kilka minut wystarcza),
- strona komunikatu: obiekt `{"id", "time", "stock", "isin", "title", "body", ...}`.

ISIN (z `stockUrlRel`) daje pewne przypisanie do spółki, bez zgadywania po nazwie.
Tytuł zaczyna się od kodu spółki ("WLT Zawarcie aneksu…"); zamieniamy go na "NAZWA: temat",
format ESPI rozumiany przez mapowanie, gdy ISIN-u nie ma w naszej liście spółek.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urljoin

import httpx

from ..clock import MARKET_TZ, to_utc
from ..config import FeedConfig
from ..models import NewsItem, SourceKind

BASE = "https://www.money.pl"
_LIST_KEY = '"stockStatementsData":{"data":'
_DETAIL = re.compile(r'\{"id":"\d+","time":"[^"]*","stock":')
_ISIN = re.compile(r"/([a-z]{2}[a-z0-9]{9}\d)\.html$", re.IGNORECASE)
_CODE = re.compile(r"^[A-Z0-9]{2,5}\s+")


def _published(local: str) -> datetime:
    """'2026-11-27 23:06' to czas warszawski; w bazie trzymamy UTC."""
    return to_utc(datetime.strptime(local, "%Y-%m-%d %H:%M"), MARKET_TZ)


def _title(stock: str, title: str) -> str:
    topic = _CODE.sub("", title, count=1).strip() or title
    return f"{stock}: {topic}" if stock else topic


def parse_list(page: str, cfg: FeedConfig) -> list[NewsItem]:
    i = page.find(_LIST_KEY)
    if i < 0:
        raise ValueError("money.pl: brak listy komunikatów na stronie (zmiana układu?)")
    rows, _ = json.JSONDecoder().raw_decode(page, i + len(_LIST_KEY))
    out = []
    for r in rows:
        m = _ISIN.search(r.get("stockUrlRel") or "")
        out.append(NewsItem(source=cfg.name, kind=SourceKind(cfg.kind), title=_title(r.get("stock", ""), r["title"]),
                            url=urljoin(BASE, r["espiUrlRel"]), published_at=_published(r["time"]),
                            priority=cfg.priority, isin=m.group(1).upper() if m else ""))
    return out


def extract_body(page: str) -> str:
    """Treść komunikatu ze strony money.pl ('' gdy to nie taka strona)."""
    m = _DETAIL.search(page)
    if not m:
        return ""
    try:
        data, _ = json.JSONDecoder().raw_decode(page, m.start())
    except ValueError:
        return ""
    return re.sub(r"\s+", " ", data.get("body") or "").strip()


class MoneyEspiSource:
    def __init__(self, cfg: FeedConfig):
        self.cfg, self.name = cfg, cfg.name

    def fetch(self) -> list[NewsItem]:
        r = httpx.get(self.cfg.url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "gpw-signals/0.1 (+news reader)"})
        r.raise_for_status()
        return parse_list(r.text, self.cfg)
