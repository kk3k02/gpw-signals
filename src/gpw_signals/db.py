"""Warstwa trwałości (SQLite). Pełny log newsów i sygnałów służy też jako ślad audytowy."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .models import Assessment, Levels, NewsItem, Signal, SignalStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    uid TEXT PRIMARY KEY,
    source TEXT, kind TEXT, title TEXT, url TEXT, summary TEXT,
    published_at TEXT, priority INTEGER, tickers TEXT,
    processed INTEGER DEFAULT 0, decision TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, company_name TEXT, created_at TEXT, news_uid TEXT,
    entry_min REAL, entry_max REAL, t1 REAL, t2 REAL, t3 REAL, stop_loss REAL,
    score REAL, assessment TEXT, status TEXT, horizon_days INTEGER,
    telegram_message_id INTEGER, closed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_ticker ON signals(ticker, created_at);
"""


class Database:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

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
        self.conn.execute("UPDATE news SET processed=1, decision=? WHERE uid=?", (decision, uid))
        self.conn.commit()

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

    def set_message_id(self, signal_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE signals SET telegram_message_id=? WHERE id=?", (message_id, signal_id))
        self.conn.commit()

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
        rows = self.conn.execute("SELECT * FROM signals WHERE closed_at IS NULL").fetchall()
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
