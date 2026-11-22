from datetime import date

from gpw_signals.config import FeedConfig
from gpw_signals.db import Database
from gpw_signals.ingest.macro import EventCalendar
from gpw_signals.ingest.rss_portals import parse_feed
from gpw_signals.mapping import TickerMap
from gpw_signals.market.regime import Regime
from gpw_signals.models import Assessment, Levels, SourceKind
from gpw_signals.scoring import score_signal
from tests.test_levels import _snap

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>ORLEN podpisał umowę na dostawy</title><link>https://ex.pl/1</link>
<description>&lt;p&gt;Spółka ORLEN zawarła istotną umowę.&lt;/p&gt;</description>
<pubDate>Mon, 16 Nov 2026 09:15:00 +0100</pubDate></item>
<item><title>DM podwyższa cenę docelową dla LPP</title><link>https://ex.pl/2</link>
<description>Rekomendacja kupuj.</description></item>
</channel></rss>"""


def test_parse_feed_and_broker_detection():
    items = parse_feed(RSS, FeedConfig(name="t", kind="portal", url="x"), ["cen docelow"])
    assert len(items) == 2
    assert items[0].summary == "Spółka ORLEN zawarła istotną umowę."
    assert items[0].kind == SourceKind.PORTAL and items[1].kind == SourceKind.BROKER


def test_dedup():
    db = Database(":memory:")
    item = parse_feed(RSS, FeedConfig(name="t", kind="espi", url="x"))[0]
    assert db.insert_news_if_new(item) is True
    assert db.insert_news_if_new(item) is False


def test_mapping():
    tm = TickerMap.from_csv("data/gpw_tickers.csv")
    assert tm.find("PKN ORLEN podpisał umowę") == ["PKN"]
    assert "LPP" in tm.find("Wyniki LPP lepsze od oczekiwań")
    assert tm.find("Pogoda w Krakowie") == []
    assert "PKO" in tm.find("PKO Bank Polski wypłaci dywidendę")


def test_calendar(tmp_path):
    p = tmp_path / "cal.yaml"
    p.write_text('"2026-12-10":\n  - "ticker:LPP Wyniki kwartalne"\n  - "Dane GUS"\n', encoding="utf-8")
    cal = EventCalendar(p)
    assert cal.earnings_within("LPP", 3, today=date(2026, 12, 8))
    assert not cal.earnings_within("PKN", 3, today=date(2026, 12, 8))
    assert len(cal.upcoming(14, "PKN", today=date(2026, 12, 1))) == 1  # tylko makro


def _a(**kw):
    d = dict(ticker="PKN", actionable=True, direction="long", catalyst_type="umowa", strength=0.8,
             horizon_days=10, already_priced_in=False, thesis="t")
    d.update(kw)
    return Assessment(**d)


LV = Levels(99.4, 100.4, [103.4, 106.4, 109.4], 96.4)


def test_score_passes_good_setup(fcfg):
    r = score_signal(_a(), _snap(), LV, Regime.BULL, SourceKind.ESPI, fcfg)
    assert r.passed, r.rejections


def test_score_rejects(fcfg):
    assert not score_signal(_a(already_priced_in=True), _snap(), LV, Regime.BULL, SourceKind.ESPI, fcfg).passed
    assert not score_signal(_a(), _snap(avg_turnover_20=1e5), LV, Regime.BULL, SourceKind.ESPI, fcfg).passed
    assert not score_signal(_a(), _snap(change_pct=9), LV, Regime.BULL, SourceKind.ESPI, fcfg).passed
    r = score_signal(_a(strength=0.5), _snap(close=90), LV, Regime.BULL, SourceKind.ESPI, fcfg)
    assert any("SMA50" in x for x in r.rejections)
    r = score_signal(_a(), _snap(), LV, Regime.BULL, SourceKind.ESPI, fcfg, earnings_soon=True)
    assert any("wyniki" in x for x in r.rejections)
