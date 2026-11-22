"""Narzędzia agenta: lokalne dane (notowania, wskaźniki, newsy, kalendarz). Kolejność i treść definicji są stałe."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..db import Database
from ..ingest.macro import EventCalendar
from ..mapping import TickerMap
from ..market.indicators import snapshot
from ..market.prices import PriceProvider


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_TICKER = {"ticker": {"type": "string", "description": "Ticker GPW, np. PKN"}}

TOOL_DEFINITIONS: list[dict] = [
    {"name": "get_price_history", "strict": True,
     "description": "Ostatnie N sesji (data, open, high, low, close, volume) dla spółki.",
     "input_schema": _schema({**_TICKER, "days": {"type": "integer", "description": "Liczba sesji, max 60"}},
                             ["ticker", "days"])},
    {"name": "get_indicators", "strict": True,
     "description": "Bieżące wskaźniki: kurs, zmiana dzienna %, ATR, SMA20/50/200, wolumen vs średnia, "
                    "obrót, maks/min 52 tyg., opory i wsparcia.",
     "input_schema": _schema(_TICKER, ["ticker"])},
    {"name": "get_company_info", "strict": True,
     "description": "Nazwa, sektor i aliasy spółki.",
     "input_schema": _schema(_TICKER, ["ticker"])},
    {"name": "get_related_news", "strict": True,
     "description": "Ostatnie newsy dotyczące spółki zapisane w bazie (do oceny, czy informacja jest nowa).",
     "input_schema": _schema(_TICKER, ["ticker"])},
    {"name": "get_upcoming_events", "strict": True,
     "description": "Zdarzenia w kalendarzu na najbliższe 14 dni: spółki (np. wyniki) i makro (RPP, GUS).",
     "input_schema": _schema(_TICKER, ["ticker"])},
]


@dataclass
class ToolContext:
    prices: PriceProvider
    tickers: TickerMap
    db: Database
    calendar: EventCalendar

    def _symbol(self, ticker: str) -> str:
        c = self.tickers.get(ticker)
        if not c:
            raise ValueError(f"Nieznany ticker {ticker}")
        return c.symbol

    def handlers(self) -> dict[str, Callable[[dict], Any]]:
        return {
            "get_price_history": self.get_price_history,
            "get_indicators": self.get_indicators,
            "get_company_info": self.get_company_info,
            "get_related_news": lambda i: self.db.related_news(i["ticker"].upper()),
            "get_upcoming_events": lambda i: self.calendar.upcoming(14, i["ticker"]),
        }

    def get_price_history(self, i: dict) -> list[dict]:
        df = self.prices.daily(self._symbol(i["ticker"]), min(int(i["days"]), 60))
        return [{"date": d.date().isoformat(), **{k: round(float(v), 4) for k, v in row.items()}}
                for d, row in df.iterrows()]

    def get_indicators(self, i: dict) -> dict:
        return snapshot(self.prices.daily(self._symbol(i["ticker"]), 260)).to_dict()

    def get_company_info(self, i: dict) -> dict:
        c = self.tickers.get(i["ticker"])
        return c.__dict__ if c else {"error": "nieznany ticker"}

    def run(self, name: str, tool_input: dict) -> tuple[str, bool]:
        """Zwraca (treść wyniku, is_error)."""
        try:
            return json.dumps(self.handlers()[name](tool_input), ensure_ascii=False, default=str), False
        except Exception as e:  # noqa: BLE001 – błąd narzędzia zwracamy modelowi jako is_error
            return f"Błąd narzędzia {name}: {e}", True
