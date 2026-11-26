"""Źródła newsów (#7): data w UTC, odrzucanie starych newsów, treść ESPI ze strony komunikatu."""
from datetime import UTC, datetime
from pathlib import Path

from gpw_signals.config import FeedConfig, Settings, load_settings
from gpw_signals.db import Database
from gpw_signals.ingest.macro import EventCalendar
from gpw_signals.ingest.rss_portals import needs_body, parse_feed
from gpw_signals.mapping import TickerMap
from gpw_signals.pipeline import App
from tests.test_calendar import _NoLlm, _NoPrices, _Source

FIX = Path(__file__).parent / "fixtures"
RSS = (FIX / "espi_title_only.xml").read_bytes()
PAGE = (FIX / "espi_page.html").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)


def _items():
    return parse_feed(RSS, FeedConfig(name="espi", kind="espi", url="x", priority=3))


def test_published_at_is_utc():
    item = _items()[0]
    assert item.published_at == datetime(2026, 11, 26, 8, 30, tzinfo=UTC)
    assert item.published_at.utcoffset().total_seconds() == 0


def test_needs_body_detects_title_only():
    toc, full = _items()
    assert needs_body(toc) and not needs_body(full)


def _app(tmp_path, fetch_body=True, pages=None):
    s = Settings()
    s.sources.fetch_espi_body = fetch_body
    s.processing.max_news_age_hours = 12
    fetched = [] if pages is None else pages
    app = App(s, Database(":memory:"), TickerMap.from_csv("data/gpw_tickers.csv"), _NoPrices(),
              EventCalendar(tmp_path / "none.yaml"), [_Source(_items())], None, assessor=_NoLlm())
    app.clock = lambda: NOW

    def page(url):
        fetched.append(url)
        return PAGE
    app.fetch_page = page
    return app, fetched


def test_old_news_dropped_at_ingest_and_body_fetched(tmp_path):
    app, fetched = _app(tmp_path)
    app.run_once()
    assert app.cycle_stats["news:za stary"] == 1
    assert app.cycle_stats["news:pobrana treść"] == 1
    assert fetched == ["https://ex.pl/wiadomosc/1"]
    rows = app.db.conn.execute("SELECT title, summary FROM news").fetchall()
    assert len(rows) == 1
    assert rows[0]["summary"].startswith("Zarząd ORLEN S.A. informuje o zawarciu umowy")
    # Kolejny cykl: news już w bazie, strony nie pobieramy ponownie
    app.run_once()
    assert fetched == ["https://ex.pl/wiadomosc/1"]


def test_body_fetch_disabled_or_failing_keeps_rss(tmp_path):
    app, fetched = _app(tmp_path, fetch_body=False)
    app.run_once()
    assert fetched == []

    app, _ = _app(tmp_path)

    def boom(url):
        raise RuntimeError("503")
    app.fetch_page = boom
    app.run_once()
    summary = app.db.conn.execute("SELECT summary FROM news").fetchone()["summary"]
    assert summary.startswith("Spis treści")


def test_settings_have_no_dead_broker_feed():
    s = load_settings()
    urls = [f.url for f in s.sources.rss]
    assert not any("rekomendacje.xml" in u for u in urls)
    assert not s.sources.fetch_espi_body       # włączane świadomie, po sprawdzeniu regulaminu
