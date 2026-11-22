"""Warstwa trwałości (SQLite). Pełny log newsów i sygnałów służy też jako ślad audytowy."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    telegram_message_id INTEGER, closed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_ticker ON signals(ticker, created_at);
CREATE INDEX IF NOT EXISTS ix_news_pending ON news(processed, published_at);
"""

# Kolumny dodane po pierwszej wersji schematu – dopisywane do istniejących baz
_MIGRATIONS = {"news": {"attempts": "INTEGER DEFAULT 0", "last_error": "TEXT"}}


class Database:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._migrate()
        self.conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        for table, cols in _MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:  # tabela jeszcze nie istnieje – utworzy ją SCHEMA
                continue
            for col, ddl in cols.items():
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        self.conn.commit()

    # --- news ---
    def insert_news_if_new(self, item: NewsItem) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO news (uid, source, kind, title, url, summary, published_at, priority, tickers)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (item.uid, item.source, item.kind.value, item.title, item.url, item.summary,
             item.published_at.isoformat(), item.priority, json.dumps(item.tickers)),
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
                         published_at=_aware(datetime.fromisoformat(r["published_at"])),
                         summary=r["summary"] or "", priority=r["priority"],
                         tickers=json.loads(r["tickers"] or "[]")) for r in rows]

    def news_row(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM news WHERE uid=?", (uid,)).fetchone()

    def related_news(self, ticker: str, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT title, kind, published_at, summary FROM news WHERE tickers LIKE ? "
            "ORDER BY published_at DESC LIMIT ?", (f'%"{ticker}"%', limit)).fetchall()
        return [dict(r) for r in rows]

    # --- signals ---
    def insert_signal(self, s: Signal) -> int:
        lv = s.levels
        cur = self.conn.execute(
            "INSERT INTO signals (ticker, company_name, created_at, news_uid, entry_min, entry_max, t1, t2, t3,"
            " stop_loss, score, assessment, status, horizon_days, telegram_message_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.ticker, s.company_name, s.created_at.isoformat(), s.news_uid, lv.entry_min, lv.entry_max,
             *lv.targets, lv.stop_loss, s.score, json.dumps(s.assessment.__dict__, ensure_ascii=False),
             s.status.value, s.assessment.horizon_days, s.telegram_message_id),
        )
        self.conn.commit()
        s.id = cur.lastrowid
        return s.id

    def update_signal_status(self, signal_id: int, status: SignalStatus, closed: bool = False) -> None:
        self.conn.execute(
            "UPDATE signals SET status=?, closed_at=? WHERE id=?",
            (status.value, datetime.now().isoformat() if closed else None, signal_id))
        self.conn.commit()

    def mark_published(self, signal_id: int, message_id: int | None) -> None:
        self.conn.execute("UPDATE signals SET telegram_message_id=?, status=? WHERE id=?",
                          (message_id, SignalStatus.ACTIVE.value, signal_id))
        self.conn.commit()

    def pending_signals(self) -> list[Signal]:
        rows = self.conn.execute("SELECT * FROM signals WHERE status=? ORDER BY created_at",
                                 (SignalStatus.PENDING_PUBLISH.value,)).fetchall()
        return [self._row_to_signal(r) for r in rows]

    def has_recent_signal(self, ticker: str, days: int) -> bool:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        row = self.conn.execute(
            "SELECT 1 FROM signals WHERE ticker=? AND (created_at>=? OR closed_at IS NULL) LIMIT 1",
            (ticker, since)).fetchone()
        return row is not None

    def signals_today(self) -> int:
        today = datetime.now().date().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM signals WHERE substr(created_at,1,10)=?", (today,)).fetchone()[0]

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
            created_at=datetime.fromisoformat(r["created_at"]), status=SignalStatus(r["status"]),
            telegram_message_id=r["telegram_message_id"],
        )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
