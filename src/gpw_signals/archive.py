"""Archiwum opublikowanych sygnałów (#16): eksport do CSV/JSON i opcjonalna retencja.

Do archiwum należą tylko sygnały, które faktycznie trafiły na kanał (mają telegram_message_id).
Retencja jest domyślnie wyłączona – nic nie jest usuwane bez jawnego `archive.retention_days`.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta

from .clock import to_iso

COLUMNS = ["id", "ticker", "company_name", "created_at", "news_uid", "entry_min", "entry_max",
           "t1", "t2", "t3", "stop_loss", "score", "status", "horizon_days", "telegram_message_id",
           "closed_at", "close_reason", "result_r", "result_pct", "thesis", "catalyst_type", "strength"]


def published_signals(conn: sqlite3.Connection, since: datetime | None = None,
                      until: datetime | None = None) -> list[dict]:
    sql = "SELECT * FROM signals WHERE telegram_message_id IS NOT NULL"
    params: list[str] = []
    if since:
        sql += " AND created_at >= ?"
        params.append(to_iso(since))
    if until:
        sql += " AND created_at < ?"
        params.append(to_iso(until))
    return [_row(r) for r in conn.execute(sql + " ORDER BY created_at, id", params).fetchall()]


def _row(r: sqlite3.Row) -> dict:
    a = json.loads(r["assessment"] or "{}")
    d = {k: r[k] for k in r.keys()}
    d.update(thesis=a.get("thesis"), catalyst_type=a.get("catalyst_type"), strength=a.get("strength"))
    return {k: d.get(k) for k in COLUMNS}


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def to_json(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2, ensure_ascii=False)


def prune(conn: sqlite3.Connection, retention_days: int | None, now: datetime) -> int:
    """Usuwa ZAMKNIĘTE sygnały starsze niż retencja. None = nic nie usuwa. Zwraca liczbę usuniętych."""
    if retention_days is None:
        return 0
    if retention_days < 1:
        raise ValueError("archive.retention_days musi być >= 1 (albo puste = bez usuwania)")
    cutoff = to_iso(now - timedelta(days=retention_days))
    cur = conn.execute("DELETE FROM signals WHERE closed_at IS NOT NULL AND created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount
