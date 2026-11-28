"""Terminy raportów okresowych z komunikatów ESPI (spółki publikują je w styczniu i przy zmianach).

Treść komunikatu jest pobierana tylko dla newsów o takim tytule. Czytamy wyłącznie polską treść raportu:
reszta strony (nawigacja, inne newsy) też zawiera daty.
"""
from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import httpx
import yaml

from .macro import EventCalendar
from .money_espi import extract_body as money_body

_TITLE = re.compile(
    r"(termin\w*|kalendarz\w*|harmonogram\w*)\b.{0,60}\b(raport\w*|sprawozda\w*)\b.{0,20}\bokresow\w*", re.IGNORECASE)
_CHANGE = re.compile(r"\bzmian", re.IGNORECASE)
_BODY = re.compile(
    r"Treść raportu:?(.*?)(?:MESSAGE\s*\(ENGLISH|INFORMACJE O PODMIOCIE|PODPISY OSÓB|$)", re.DOTALL | re.IGNORECASE)
_SKIP = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")

_MONTHS = {m: i for i, m in enumerate(
    ("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
     "października", "listopada", "grudnia"), start=1)}
_DATE = re.compile(
    rf"\b(?:(?P<d1>\d{{1,2}})\s+(?P<mon>{'|'.join(_MONTHS)})\s+(?P<y1>20\d\d)"
    r"|(?P<y2>20\d\d)-(?P<m2>\d\d)-(?P<d2>\d\d)"
    r"|(?P<d3>\d{1,2})\.(?P<m3>\d{1,2})\.(?P<y3>20\d\d))", re.IGNORECASE)
# "z [terminu pierwotnie wyznaczonego na] STARA, na dzień NOWA" – tekst między datami tej samej pary
_PAIR = re.compile(r"^[\s,]*(?:r\.|roku)?[\s,]*(?:na|do)(?:\s+dzień)?\s*$", re.IGNORECASE)
_MAX_AHEAD = timedelta(days=430)


class ScheduleError(ValueError):
    """Komunikat pobrany, ale bez rozpoznawalnych terminów."""


def is_report_schedule(title: str) -> bool:
    return _TITLE.search(title) is not None


def is_change(title: str) -> bool:
    return _CHANGE.search(title) is not None


def extract_body(page: str) -> str:
    """Polska treść raportu ze strony komunikatu ('' gdy jej nie ma)."""
    text = html.unescape(_TAG.sub(" ", _SKIP.sub(" ", page))).replace("\xa0", " ")
    m = _BODY.search(text)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else money_body(page)


def _to_date(m: re.Match) -> date | None:
    g = m.groupdict()
    try:
        if g["mon"]:
            return date(int(g["y1"]), _MONTHS[g["mon"].lower()], int(g["d1"]))
        if g["y2"]:
            return date(int(g["y2"]), int(g["m2"]), int(g["d2"]))
        return date(int(g["y3"]), int(g["m3"]), int(g["d3"]))
    except ValueError:
        return None


def parse_dates(body: str, published: date) -> tuple[set[date], set[date]]:
    """(terminy, terminy odwołane zmianą). Daty do dnia publikacji (np. rozporządzeń) są pomijane."""
    found = [(m, d) for m in _DATE.finditer(body) if (d := _to_date(m))]
    current, replaced = set(), set()
    for i, (m, d) in enumerate(found):
        if not published < d <= published + _MAX_AHEAD:
            continue
        is_old = i + 1 < len(found) and _PAIR.match(body[m.end():found[i + 1][0].start()])
        (replaced if is_old else current).add(d)
    return current, replaced - current


def fetch_page(url: str) -> str:
    r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "gpw-signals/0.1 (+rss reader)"})
    r.raise_for_status()
    return r.text


class ReportCalendar:
    """Plik generowany (data -> ["ticker:XXX Wyniki okresowe"]), w formacie czytanym przez EventCalendar."""

    EVENT = "Wyniki okresowe"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def update(self, ticker: str, dates: Iterable[date], replaced: Iterable[date] = (),
               clear_from: date | None = None) -> None:
        """Dopisuje terminy spółki. `clear_from`: pełny harmonogram zastępuje jej terminy od tego dnia."""
        entry, drop = f"ticker:{ticker.upper()} {self.EVENT}", set(replaced)
        events = EventCalendar(self.path).events
        for d, items in events.items():
            if entry in items and (d in drop or (clear_from and d >= clear_from)):
                items.remove(entry)
        for d in dates:
            items = events.setdefault(d, [])
            if entry not in items:
                items.append(entry)
        data = {d.isoformat(): sorted(events[d]) for d in sorted(events) if events[d]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self.path)

    def import_announcement(self, ticker: str, title: str, body: str, published: date) -> set[date]:
        dates, replaced = parse_dates(body, published)
        if not dates:
            raise ScheduleError("brak terminów w treści komunikatu")
        self.update(ticker, dates, replaced, None if is_change(title) else published)
        return dates
