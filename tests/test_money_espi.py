"""Raporty ESPI/EBI wszystkich spółek z money.pl (#7) – na zapisanych stronach."""
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gpw_signals.config import FeedConfig
from gpw_signals.ingest.money_espi import extract_body, parse_list
from gpw_signals.ingest.report_schedule import extract_body as page_body
from gpw_signals.mapping import TickerMap
from gpw_signals.models import SourceKind

FIX = Path(__file__).parent / "fixtures"
LIST = (FIX / "money_list.html").read_text(encoding="utf-8")
DETAIL = (FIX / "money_detail.html").read_text(encoding="utf-8")
CFG = FeedConfig(name="money-espi", kind="espi", url="https://www.money.pl/gielda/komunikaty/", priority=3,
                 format="money")


def _items():
    return parse_list(LIST, CFG)


def test_parse_list_fields():
    first = _items()[0]
    assert first.kind == SourceKind.ESPI and first.priority == 3 and first.source == "money-espi"
    assert first.title.startswith("MULTIQURE: Uchwały Nadzwyczajnego Walnego Zgromadzenia")
    assert first.url == "https://www.money.pl/gielda/komunikaty/7355668562641473.html"
    assert first.isin == "PLPRBLG00010"
    assert first.summary == ""  # treść dociąga pipeline (fetch_espi_body)


def test_published_is_warsaw_time_in_utc():
    assert _items()[0].published_at == datetime(2026, 11, 27, 21, 33, tzinfo=UTC)  # 22:33 CET


def test_title_keeps_report_topic_after_code():
    assert _items()[2].title == "REDAN: QS 3/2026: formularz skonsolidowanego raportu kwartalnego"


def test_mapping_by_isin_and_name_fallback():
    tm = TickerMap.from_csv("data/gpw_tickers.csv")
    mq, pcx, rdn = _items()
    assert tm.map_news(mq.title, "", mq.kind, mq.isin) == ["MULTIQURE"]
    assert tm.map_news(pcx.title, "", pcx.kind, pcx.isin) == ["PCCEXOL"]
    assert tm.map_news(rdn.title, "", rdn.kind, rdn.isin) == []  # spoza naszej listy spółek


def test_detail_body():
    body = extract_body(DETAIL)
    assert body.startswith("Zarząd KBJ S.A. [Spółka, Emitent] informuje")
    assert "\n" not in body
    assert page_body(DETAIL) == body  # pipeline używa wspólnego extract_body


def test_layout_change_is_an_error_not_silence():
    with pytest.raises(ValueError):
        parse_list("<html>nowy układ</html>", CFG)
    assert extract_body("<html></html>") == ""
