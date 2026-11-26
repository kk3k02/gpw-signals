"""Monitoring (#15): alerty, znacznik życia, raport dzienny, logi, praca bazy w wątku schedulera."""
import json
import logging
import threading
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from gpw_signals.__main__ import main
from gpw_signals.agent.assess import AgentError
from gpw_signals.config import LoggingConfig, Settings
from gpw_signals.db import Database
from gpw_signals.ingest.base import fetch_all
from gpw_signals.mapping import TickerMap
from gpw_signals.models import Assessment, Levels, Signal
from gpw_signals.monitor import (
    Monitor,
    format_daily_report,
    heartbeat_problem,
    setup_logging,
    write_heartbeat,
)
from gpw_signals.pipeline import App
from gpw_signals.scheduler import make_scheduler, poll_tick

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
PL = ZoneInfo("Europe/Warsaw")


# ------------------------------------------------------------------ alerty

def test_alert_once_after_threshold_and_once_on_recovery():
    sent = []
    m = Monitor(sent.append, threshold=3)
    for _ in range(5):
        m.fail("źródło x", "timeout")
    assert len(sent) == 1 and "3 z rzędu" in sent[0] and "timeout" in sent[0]
    m.ok("źródło x")
    m.ok("źródło x")
    assert len(sent) == 2 and "działa ponownie" in sent[1]


def test_success_resets_counter_and_keys_are_independent():
    sent = []
    m = Monitor(sent.append, threshold=2)
    m.fail("a", "e")
    m.ok("a")
    m.fail("a", "e")
    m.fail("b", "e")
    assert sent == []
    m.fail("c", "e", threshold=1)
    assert len(sent) == 1


def test_failed_alert_does_not_raise_and_is_retried():
    calls = []

    def notify(text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("telegram")
    m = Monitor(notify, threshold=1)
    m.fail("a", "e")
    m.fail("a", "e")
    m.fail("a", "e")
    assert len(calls) == 2


def test_fetch_all_reports_each_source():
    def src(name, err=None):
        def fetch():
            if err:
                raise err
            return []
        return SimpleNamespace(name=name, fetch=fetch)
    seen = []
    fetch_all([src("a"), src("b", RuntimeError("503"))], lambda name, e: seen.append((name, e and str(e))))
    assert seen == [("a", None), ("b", "503")]


# ------------------------------------------------------------------ App

class _Boom:
    def __getattr__(self, name):
        def f(*a, **k):
            raise RuntimeError("offline")
        return f


def _app(tmp_path, **kw):
    s = Settings()
    s.monitoring.heartbeat_file = str(tmp_path / "hb")
    sent = []
    app = App(s, Database(":memory:"), TickerMap.from_csv("data/gpw_tickers.csv"), _Boom(), None,
              kw.get("sources", []), _Boom(), clock=lambda: NOW, monitor=Monitor(sent.append, threshold=3))
    return app, sent


def test_source_outage_alerts_after_three_cycles(tmp_path):
    src = SimpleNamespace(name="espi", fetch=_Boom().fetch)
    app, sent = _app(tmp_path, sources=[src])
    for _ in range(3):
        app.run_once()
    assert [t for t in sent if "źródło espi" in t]
    assert [t for t in sent if "notowania indeksu" in t]
    src.fetch = lambda: []
    app.run_once()
    assert "źródło espi: działa ponownie" in sent[-1]


def test_publish_failure_alerts_immediately(tmp_path):
    app, sent = _app(tmp_path)
    a = Assessment("PKN", True, "long", "umowa", 0.7, 10, False, "x")
    assert not app._publish(Signal("PKN", "ORLEN", Levels(59, 60, [62, 64, 66], 58), 0.7, a, "u", NOW))
    assert len(sent) == 1 and "publikacja" in sent[0]


@pytest.mark.parametrize("error", [AgentError("odmowa"), RuntimeError("401")])
def test_agent_errors_are_counted(tmp_path, error):
    app, sent = _app(tmp_path)

    def assess(*a):
        raise error
    app.assessor = SimpleNamespace(assess=assess)
    for _ in range(3):
        with pytest.raises(type(error)):
            app._assess(None, "PKN", "ORLEN")
    assert len(sent) == 1 and "agent" in sent[0]


def test_daily_report_is_sent_to_admin(tmp_path):
    app, sent = _app(tmp_path)
    app.db.add_stage_counts(date(2026, 11, 24), {"llm": 9})
    app.daily_report()
    assert "Raport 2026-11-25" in sent[0] and "Brak przetworzonych" in sent[0]
    app.db.add_stage_counts(date(2026, 11, 25), {"news:nowe": 7, "llm": 2, "bez_llm:szum": 4, "sygnał": 1})
    app.daily_report()
    assert "Nowe newsy: 7, wywołania LLM: 2" in sent[1] and "szum: 4" in sent[1]


def test_report_with_unknown_model_cost():
    rep = {"stages": {}, "cost_usd": None,
           "models": {"x": {"requests": 1, "input": 5, "output": 2, "cache_read": 0, "cache_write": 0}}}
    assert "Tokeny: 7, koszt: nieznany" in format_daily_report("2026-11-25", rep, 0)


# ------------------------------------------------------------------ scheduler i healthcheck

def test_heartbeat(tmp_path):
    p, limit = tmp_path / "d" / "hb", timedelta(minutes=30)
    assert "brak" in heartbeat_problem(p, NOW, limit)
    write_heartbeat(p, NOW)
    assert heartbeat_problem(p, NOW + timedelta(minutes=30), limit) is None
    assert "31 min" in heartbeat_problem(p, NOW + timedelta(minutes=31), limit)
    p.write_text("śmieci", encoding="utf-8")
    assert "brak" in heartbeat_problem(p, NOW, limit)


def test_poll_tick_beats_on_success_and_outside_session_but_not_on_failure(tmp_path):
    app, sent = _app(tmp_path)
    hb = tmp_path / "hb"
    in_session = datetime(2026, 11, 25, 12, 0, tzinfo=PL)
    app.run_once = _Boom().run_once
    for _ in range(3):
        poll_tick(app, in_session)
    assert not hb.exists() and "cykl" in sent[0]
    poll_tick(app, datetime(2026, 11, 28, 12, 0, tzinfo=PL))   # sobota: bot żyje, tylko nie pracuje
    assert hb.exists()
    hb.unlink()
    app.run_once = lambda: []
    poll_tick(app, in_session)
    assert hb.exists() and "działa ponownie" in sent[-1]


def test_database_works_in_scheduler_thread():
    db, out = Database(":memory:"), []
    t = threading.Thread(target=lambda: out.append(db.pending_news()))
    t.start()
    t.join()
    assert out == [[]]


def test_scheduler_runs_one_job_at_a_time_and_does_not_drop_late_jobs():
    sched = make_scheduler(PL)
    running, overlaps, done = [], [], threading.Event()

    def job(i):
        overlaps.append(bool(running))
        running.append(i)
        threading.Event().wait(0.2)
        running.remove(i)
        if len(overlaps) == 3:
            done.set()
            sched.shutdown(wait=False)
    for i in range(3):
        sched.add_job(job, args=[i])
    late = sched.add_job(lambda: None, "interval", hours=1)
    threading.Timer(10, lambda: None if done.is_set() else sched.shutdown(wait=False)).start()
    sched.start()
    assert overlaps == [False] * 3 and late.misfire_grace_time >= 3600


def test_health_command(tmp_path, capsys):
    cfg, hb = tmp_path / "s.yaml", tmp_path / "hb"
    cfg.write_text(f"monitoring:\n  heartbeat_file: '{hb.as_posix()}'\n", encoding="utf-8")
    assert main(["--config", str(cfg), "health"]) == 1
    write_heartbeat(hb, datetime.now(UTC))
    assert main(["--config", str(cfg), "health"]) == 0


# ------------------------------------------------------------------ logi

@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    root.handlers = []
    yield root
    for h in root.handlers:
        h.close()
    root.handlers = saved
    root.setLevel(level)


def test_json_log_file_with_rotation_and_no_http_urls(tmp_path, restore_logging):
    f = tmp_path / "logs" / "bot.log"
    setup_logging(LoggingConfig(json_format=True, file=str(f), max_mb=0.001, backups=2), to_file=True)
    logging.getLogger("httpx").info("POST https://api.telegram.org/botSEKRET/sendMessage")
    for i in range(50):
        logging.getLogger("x").info("zażółć %d", i)
    files = sorted(p.name for p in f.parent.iterdir())
    assert files == ["bot.log", "bot.log.1", "bot.log.2"]
    text = "".join(p.read_text(encoding="utf-8") for p in f.parent.iterdir())
    assert "SEKRET" not in text
    assert json.loads(f.read_text(encoding="utf-8").splitlines()[-1])["message"] == "zażółć 49"


def test_no_log_file_outside_run(tmp_path, restore_logging):
    f = tmp_path / "bot.log"
    setup_logging(LoggingConfig(file=str(f)))
    assert not f.exists()
