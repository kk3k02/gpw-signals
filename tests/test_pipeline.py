"""Testy integracyjne App: kolejka newsów z ponawianiem (#1) i zapis sygnału przed publikacją (#2)."""
from datetime import UTC, datetime, timedelta

import anthropic
import httpx2
import pytest

from gpw_signals.agent.assess import AgentError
from gpw_signals.config import FiltersConfig, LevelsConfig, ProcessingConfig, PublishConfig, Settings
from gpw_signals.db import Database
from gpw_signals.ingest.macro import EventCalendar
from gpw_signals.mapping import TickerMap
from gpw_signals.models import Assessment, NewsItem, SignalStatus, SourceKind
from gpw_signals.pipeline import App
from tests.conftest import make_ohlcv


class FakePrices:
    def __init__(self, fail: int = 0):
        self.fail = fail

    def daily(self, symbol, days=250):
        if self.fail:
            self.fail -= 1
            raise OSError("timeout")
        return make_ohlcv().tail(days)


class FakeAssessor:
    """Kolejne wywołania zwracają/rzucają elementy z `script`; po wyczerpaniu – dobra ocena."""

    def __init__(self, *script):
        self.script, self.calls = list(script), 0

    def assess(self, news, ticker, company):
        self.calls += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
        return Assessment(ticker, True, "long", "umowa", 0.8, 10, False, "Istotna umowa.")


class FakePublisher:
    def __init__(self, fail: int = 0):
        self.fail, self.sent = fail, []

    def send(self, text, reply_to=None):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("Telegram niedostępny")
        self.sent.append(text)
        return 100 + len(self.sent)


class FakeSource:
    name = "fake"

    def __init__(self, items):
        self.items = items

    def fetch(self):
        out, self.items = self.items, []  # każdy news tylko w pierwszym cyklu, jak w RSS po rotacji
        return out


def _news(title="ORLEN podpisał istotną umowę", age=timedelta(minutes=5)):
    return NewsItem("fake", SourceKind.ESPI, title, f"https://ex.pl/{hash(title)}",
                    datetime.now(UTC) - age, "Treść komunikatu.", priority=3)


def _settings():
    # Filtry poluzowane, żeby test sprawdzał przepływ, a nie progi rynkowe
    return Settings(
        levels=LevelsConfig(resistance_snap_r=0.0),
        filters=FiltersConfig(min_avg_turnover_pln=0, min_rr_t1=0.0, require_above_sma50=False,
                              block_bear_regime=False, min_volume_ratio=0, max_gap_pct=100, min_score=0.0),
        processing=ProcessingConfig(max_news_age_hours=12, max_attempts=3),
        publish=PublishConfig(max_pending_minutes=60, disclaimer=""),
    )


@pytest.fixture
def make_app(tmp_path):
    def _make(items, prices=None, assessor=None, publisher=None):
        cal = tmp_path / "cal.yaml"
        cal.write_text("{}", encoding="utf-8")
        app = App(_settings(), Database(":memory:"), TickerMap.from_csv("data/gpw_tickers.csv"),
                  prices or FakePrices(), EventCalendar(cal), [FakeSource(items)],
                  publisher or FakePublisher())
        app.assessor = assessor or FakeAssessor()
        return app
    return _make


def _row(app, news):
    return app.db.news_row(news.uid)


# ------------------------------------------------------------------ #1 kolejka newsów

def test_happy_path_publishes_once(make_app):
    n = _news()
    app = make_app([n])
    sigs = app.run_once()
    assert len(sigs) == 1 and len(app.publisher.sent) == 1
    assert sigs[0].status == SignalStatus.ACTIVE and sigs[0].telegram_message_id == 101
    assert _row(app, n)["processed"] == 1 and "SYGNAŁ" in _row(app, n)["decision"]
    assert app.run_once() == []  # drugi cykl nic nie dubluje


def test_transient_api_error_is_retried(make_app):
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    n = _news()
    app = make_app([n], assessor=FakeAssessor(err))
    assert app.run_once() == []
    row = _row(app, n)
    assert row["processed"] == 0 and row["attempts"] == 1 and "API Claude" in row["last_error"]

    sigs = app.run_once()  # news nie przychodzi już z RSS, ale wraca z kolejki w bazie
    assert len(sigs) == 1 and app.assessor.calls == 2


def test_price_outage_is_retried(make_app):
    n = _news()
    app = make_app([n], prices=FakePrices(fail=2))  # 1. WIG20 (reżim), 2. notowania spółki
    app.run_once()
    assert _row(app, n)["processed"] == 0
    assert len(app.run_once()) == 1


def test_final_agent_error_is_not_retried(make_app):
    n = _news()
    app = make_app([n], assessor=FakeAssessor(AgentError("Model odmówił oceny")))
    app.run_once()
    row = _row(app, n)
    assert row["processed"] == 1 and "odmówił" in row["decision"]
    app.run_once()
    assert app.assessor.calls == 1


def test_gives_up_after_max_attempts(make_app):
    n = _news()
    app = make_app([n], prices=FakePrices(fail=10))
    for _ in range(3):
        app.run_once()
    row = _row(app, n)
    assert row["processed"] == 1 and row["attempts"] == 3 and row["decision"].startswith("porzucony")


def test_unexpected_error_does_not_stop_cycle(make_app):
    bad, good = _news("ORLEN zawarł umowę"), _news("PKO BP podał wyniki")
    app = make_app([bad, good], assessor=FakeAssessor(KeyError("bug")))
    sigs = app.run_once()
    assert [s.ticker for s in sigs] == ["PKO"]
    assert _row(app, bad)["processed"] == 0 and _row(app, bad)["attempts"] == 1


def test_news_left_unprocessed_by_crash_is_picked_up(make_app):
    n = _news()
    app = make_app([])
    n.tickers = ["PKN"]
    app.db.insert_news_if_new(n)  # stan po crashu: news w bazie, processed=0
    assert len(app.run_once()) == 1
    assert _row(app, n)["processed"] == 1


def test_stale_pending_news_is_skipped(make_app):
    n = _news(age=timedelta(hours=13))
    app = make_app([n])
    assert app.run_once() == []
    assert _row(app, n)["decision"] == "nieaktualny" and app.assessor.calls == 0


def test_migrates_old_schema(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE news (uid TEXT PRIMARY KEY, source TEXT, kind TEXT, title TEXT, url TEXT, "
                "summary TEXT, published_at TEXT, priority INTEGER, tickers TEXT, "
                "processed INTEGER DEFAULT 0, decision TEXT)")
    con.commit()
    con.close()
    db = Database(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(news)")}
    assert {"attempts", "last_error"} <= cols


# ------------------------------------------------------------------ #2 zapis przed publikacją

def test_publish_failure_keeps_signal_and_retries_without_duplicate(make_app):
    n = _news()
    app = make_app([n], publisher=FakePublisher(fail=1))
    assert app.run_once() == []
    [row] = app.db.all_signals()
    assert row["status"] == "pending_publish" and row["telegram_message_id"] is None
    assert _row(app, n)["processed"] == 1  # news rozstrzygnięty – nie wraca do agenta
    assert app.db.has_recent_signal("PKN", 5)  # cooldown działa od razu
    assert app.db.open_signals() == []          # tracker nie śledzi nieopublikowanych

    app.run_once()  # kolejny cykl ponawia wysyłkę z bazy
    [row] = app.db.all_signals()
    assert row["status"] == "active" and row["telegram_message_id"] == 101
    assert len(app.publisher.sent) == 1 and app.assessor.calls == 1
    assert len(app.db.open_signals()) == 1


def test_stale_pending_signal_is_not_sent(make_app):
    n = _news()
    app = make_app([n], publisher=FakePublisher(fail=1))
    app.run_once()
    sid = app.db.all_signals()[0]["id"]
    app.db.conn.execute("UPDATE signals SET created_at=? WHERE id=?",
                        ((datetime.now() - timedelta(minutes=61)).isoformat(), sid))
    app.run_once()
    [row] = app.db.all_signals()
    assert row["status"] == "unpublished" and row["closed_at"] is not None
    assert app.publisher.sent == []
