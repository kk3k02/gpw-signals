"""Warstwa trwałości (SQLite). Pełny log newsów i sygnałów służy też jako ślad audytowy."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .agent.usage import TokenUsage
from .clock import from_iso, to_iso
from .models import Assessment, Levels, NewsItem, Signal, SignalStatus, SourceKind

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    uid TEXT PRIMARY KEY,
    source TEXT, kind TEXT, title TEXT, url TEXT, summary TEXT,
    published_at TEXT, priority INTEGER, tickers TEXT,
    processed INTEGER DEFAULT 0, decision TEXT,
    attempts INTEGER DEFAULT 0, last_error TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, company_name TEXT, created_at TEXT, news_uid TEXT,
    entry_min REAL, entry_max REAL, t1 REAL, t2 REAL, t3 REAL, stop_loss REAL,
    score REAL, assessment TEXT, status TEXT, horizon_days INTEGER,
    telegram_message_id INTEGER, closed_at TEXT,
    close_reason TEXT, result_r REAL, result_pct REAL, last_outcome_on TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage (
    day TEXT, model TEXT, requests INTEGER, input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
    PRIMARY KEY (day, model)
);
CREATE TABLE IF NOT EXISTS stage_counts (
    day TEXT, stage TEXT, n INTEGER, PRIMARY KEY (day, stage)
);
CREATE INDEX IF NOT EXISTS ix_signals_ticker ON signals(ticker, created_at);
CREATE INDEX IF NOT EXISTS ix_news_pending ON news(processed, published_at);
"""

# Kolumny dodane po pierwszej wersji schematu – dopisywane do istniejących baz
_MIGRATIONS = {"news": {"attempts": "INTEGER DEFAULT 0", "last_error": "TEXT"},
               "signals": {"close_reason": "TEXT", "result_r": "REAL", "result_pct": "REAL",
                           "last_outcome_on": "TEXT"}}

# Wersja formatu danych (PRAGMA user_version). 1 = wszystkie daty w UTC w formacie clock.to_iso.
_DATA_VERSION = 1
_DATE_COLUMNS = {"news": ["published_at"], "signals": ["created_at", "closed_at"]}


class Database:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._migrate()
        self.conn.executescript(SCHEMA)
        self._migrate_dates()

    def _migrate(self) -> None:
        for table, cols in _MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:  # tabela jeszcze nie istnieje – utworzy ją SCHEMA
                continue
            for col, ddl in cols.items():
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        self.conn.commit()

    def _migrate_dates(self) -> None:
        """Jednorazowo: daty bez strefy z wcześniejszych wersji (zapisane jako czas lokalny serwera)
        przelicza na UTC, a wszystkie daty sprowadza do jednego formatu."""
        if self.conn.execute("PRAGMA user_version").fetchone()[0] >= _DATA_VERSION:
            return
        for table, cols in _DATE_COLUMNS.items():
            for col in cols:
                rows = self.conn.execute(f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
                for rowid, value in rows:
                    dt = datetime.fromisoformat(value)
                    # naiwna data = datetime.now() serwera -> astimezone() interpretuje ją jako czas lokalny
                    fixed = to_iso(dt if dt.tzinfo else dt.astimezone())
                    if fixed != value:
                        self.conn.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (fixed, rowid))
        self.conn.execute(f"PRAGMA user_version = {_DATA_VERSION}")
        self.conn.commit()

    # --- news ---
    def insert_news_if_new(self, item: NewsItem) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO news (uid, source, kind, title, url, summary, published_at, priority, tickers)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (item.uid, item.source, item.kind.value, item.title, item.url, item.summary,
             to_iso(item.published_at), item.priority, json.dumps(item.tickers)),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def mark_news(self, uid: str, decision: str) -> None:
        """Decyzja ostateczna – news nie wróci do kolejki."""
        self.conn.execute("UPDATE news SET processed=1, decision=? WHERE uid=?", (decision, uid))
        self.conn.commit()

    def record_failure(self, uid: str, error: str, max_attempts: int) -> bool:
        """Błąd przejściowy: zwiększa licznik prób. Zwraca True, gdy news został porzucony (limit prób)."""
        self.conn.execute("UPDATE news SET attempts=attempts+1, last_error=? WHERE uid=?", (error[:500], uid))
        attempts = self.conn.execute("SELECT attempts FROM news WHERE uid=?", (uid,)).fetchone()[0]
        abandoned = attempts >= max_attempts
        if abandoned:
            self.conn.execute("UPDATE news SET processed=1, decision=? WHERE uid=?",
                              (f"porzucony po {attempts} próbach: {error[:200]}", uid))
        self.conn.commit()
        return abandoned

    def pending_news(self) -> list[NewsItem]:
        """Wszystkie nieprzetworzone newsy – nowe i zaległe (np. po crashu albo błędzie przejściowym)."""
        rows = self.conn.execute(
            "SELECT * FROM news WHERE processed=0 ORDER BY priority DESC, published_at DESC").fetchall()
        return [NewsItem(source=r["source"], kind=SourceKind(r["kind"]), title=r["title"], url=r["url"],
                         published_at=from_iso(r["published_at"]),
                         summary=r["summary"] or "", priority=r["priority"],
                         tickers=json.loads(r["tickers"] or "[]")) for r in rows]

    def news_row(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM news WHERE uid=?", (uid,)).fetchone()

    def related_news(self, ticker: str, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT title, kind, published_at, summary FROM news WHERE tickers LIKE ? "
            "ORDER BY published_at DESC LIMIT ?", (f'%"{ticker}"%', limit)).fetchall()
        return [dict(r) for r in rows]

    # --- liczniki dzienne (#6) ---
    def add_usage(self, day: date, model: str, u: TokenUsage) -> None:
        self.conn.execute(
            "INSERT INTO llm_usage VALUES (?,?,1,?,?,?,?) ON CONFLICT(day, model) DO UPDATE SET"
            " requests=requests+1, input=input+excluded.input, output=output+excluded.output,"
            " cache_read=cache_read+excluded.cache_read, cache_write=cache_write+excluded.cache_write",
            (day.isoformat(), model, u.input, u.output, u.cache_read, u.cache_write))
        self.conn.commit()

    def add_stage_counts(self, day: date, counts: dict[str, int]) -> None:
        self.conn.executemany(
            "INSERT INTO stage_counts VALUES (?,?,?) ON CONFLICT(day, stage) DO UPDATE SET n=n+excluded.n",
            [(day.isoformat(), k, n) for k, n in counts.items()])
        self.conn.commit()

    def daily_report(self, limit: int = 14) -> list[dict]:
        """Najnowsze dni: decyzje wg etapu, tokeny i szacowany koszt (None, gdy model spoza cennika)."""
        days = [r[0] for r in self.conn.execute(
            "SELECT day FROM stage_counts UNION SELECT day FROM llm_usage ORDER BY day DESC LIMIT ?", (limit,))]
        out = []
        for day in days:
            stages = {r["stage"]: r["n"] for r in self.conn.execute(
                "SELECT stage, n FROM stage_counts WHERE day=? ORDER BY stage", (day,))}
            models, costs = {}, []
            for r in self.conn.execute("SELECT * FROM llm_usage WHERE day=?", (day,)):
                u = TokenUsage(r["input"], r["output"], r["cache_read"], r["cache_write"])
                models[r["model"]] = {"requests": r["requests"], **u.__dict__}
                costs.append(u.cost_usd(r["model"]))
            out.append({"day": day, "stages": stages, "models": models,
                        "cost_usd": None if None in costs else round(sum(costs), 4)})
        return out

    # --- signals ---
    def insert_signal(self, s: Signal) -> int:
        lv = s.levels
        cur = self.conn.execute(
            "INSERT INTO signals (ticker, company_name, created_at, news_uid, entry_min, entry_max, t1, t2, t3,"
            " stop_loss, score, assessment, status, horizon_days, telegram_message_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.ticker, s.company_name, to_iso(s.created_at), s.news_uid, lv.entry_min, lv.entry_max,
             *lv.targets, lv.stop_loss, s.score, json.dumps(s.assessment.__dict__, ensure_ascii=False),
             s.status.value, s.assessment.horizon_days, s.telegram_message_id),
        )
        self.conn.commit()
        s.id = cur.lastrowid
        return s.id

    def update_signal_status(self, signal_id: int, status: SignalStatus, closed_at: datetime | None = None) -> None:
        """`closed_at` podane = sygnał zamknięty w tej chwili; None = nadal otwarty."""
        self.conn.execute(
            "UPDATE signals SET status=?, closed_at=? WHERE id=?",
            (status.value, to_iso(closed_at) if closed_at else None, signal_id))
        self.conn.commit()

    def record_outcome(self, signal_id: int, status: SignalStatus, on: date, closed_at: datetime | None = None,
                       reason: str | None = None, result_r: float | None = None,
                       result_pct: float | None = None) -> None:
        """Zmiana wyniku z trackera; `on` = dzień rynku, w którym ją zapisano (maks. jedna dziennie)."""
        self.conn.execute(
            "UPDATE signals SET status=?, closed_at=?, close_reason=?, result_r=?, result_pct=?, last_outcome_on=?"
            " WHERE id=?",
            (status.value, to_iso(closed_at) if closed_at else None, reason, result_r, result_pct,
             on.isoformat(), signal_id))
        self.conn.commit()

    def outcome_recorded_on(self, signal_id: int, day: date) -> bool:
        return self.conn.execute("SELECT 1 FROM signals WHERE id=? AND last_outcome_on=?",
                                 (signal_id, day.isoformat())).fetchone() is not None

    def closed_signals_since(self, since: datetime) -> list[sqlite3.Row]:
        """Opublikowane sygnały zamknięte od `since`."""
        return self.conn.execute("SELECT * FROM signals WHERE closed_at>=? AND status<>? ORDER BY closed_at",
                                 (to_iso(since), SignalStatus.UNPUBLISHED.value)).fetchall()

    def mark_published(self, signal_id: int, message_id: int | None) -> None:
        self.conn.execute("UPDATE signals SET telegram_message_id=?, status=? WHERE id=?",
                          (message_id, SignalStatus.ACTIVE.value, signal_id))
        self.conn.commit()

    def pending_signals(self) -> list[Signal]:
        rows = self.conn.execute("SELECT * FROM signals WHERE status=? ORDER BY created_at",
                                 (SignalStatus.PENDING_PUBLISH.value,)).fetchall()
        return [self._row_to_signal(r) for r in rows]

    def has_recent_signal(self, ticker: str, since: datetime) -> bool:
        """Czy spółka ma sygnał otwarty albo utworzony od `since` (cooldown)."""
        row = self.conn.execute(
            "SELECT 1 FROM signals WHERE ticker=? AND (created_at>=? OR closed_at IS NULL) LIMIT 1",
            (ticker, to_iso(since))).fetchone()
        return row is not None

    def count_signals_between(self, start: datetime, end: datetime) -> int:
        """Liczba sygnałów utworzonych w [start, end) – np. w granicach dnia sesji z clock.market_day_bounds."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at>=? AND created_at<?",
            (to_iso(start), to_iso(end))).fetchone()[0]

    def open_signals(self) -> list[Signal]:
        # Tracker śledzi tylko sygnały faktycznie opublikowane
        rows = self.conn.execute("SELECT * FROM signals WHERE closed_at IS NULL AND status NOT IN (?, ?)",
                                 (SignalStatus.PENDING_PUBLISH.value, SignalStatus.UNPUBLISHED.value)).fetchall()
        return [self._row_to_signal(r) for r in rows]

    def all_signals(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM signals ORDER BY created_at").fetchall()

    @staticmethod
    def _row_to_signal(r: sqlite3.Row) -> Signal:
        a = Assessment(**json.loads(r["assessment"]))
        return Signal(
            id=r["id"], ticker=r["ticker"], company_name=r["company_name"],
            levels=Levels(r["entry_min"], r["entry_max"], [r["t1"], r["t2"], r["t3"]], r["stop_loss"]),
            score=r["score"], assessment=a, news_uid=r["news_uid"],
            created_at=from_iso(r["created_at"]), status=SignalStatus(r["status"]),
            telegram_message_id=r["telegram_message_id"],
        )
