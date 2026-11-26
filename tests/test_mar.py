"""#16: metodologia w sygnale, konflikty interesów, archiwum i retencja."""
import csv
import io
import json
from datetime import UTC, datetime

from gpw_signals import archive
from gpw_signals.__main__ import main
from gpw_signals.db import Database
from gpw_signals.models import Assessment, Levels, Signal, SignalStatus
from gpw_signals.publish.formatter import conflict_note_for, format_signal


def _signal(ticker="PKN", created=datetime(2026, 11, 2, 10, 0, tzinfo=UTC)) -> Signal:
    a = Assessment(ticker, True, "long", "umowa", 0.7, 10, False, "Duży kontrakt.")
    return Signal(ticker, "ORLEN", Levels(60.0, 61.0, [63.0, 65.0, 67.0], 58.0), 0.7, a, "u1", created,
                  status=SignalStatus.PENDING_PUBLISH)


def test_format_with_conflict_and_methodology():
    txt = format_signal(_signal(), include_thesis=False, disclaimer="Disclaimer.",
                        methodology_url="https://example.org/metodologia", conflict_note="Autor ma pozycję.")
    assert txt.endswith("Autor ma pozycję.\n\nDisclaimer.\nMetodologia: https://example.org/metodologia")


def test_conflict_note_only_for_listed_tickers():
    assert conflict_note_for("pkn", [" PKN "], "X") == "X"
    assert conflict_note_for("CDR", ["PKN"], "X") == ""


def _db_with_signals() -> Database:
    db = Database(":memory:")
    published = _signal()
    db.insert_signal(published)
    db.mark_published(published.id, 101)
    db.record_outcome(published.id, SignalStatus.STOPPED, datetime(2026, 11, 5).date(),
                      closed_at=datetime(2026, 11, 5, 17, 0, tzinfo=UTC))
    db.insert_signal(_signal("CDR", datetime(2026, 11, 20, 10, 0, tzinfo=UTC)))  # nieopublikowany
    return db


def test_export_only_published():
    db = _db_with_signals()
    rows = archive.published_signals(db.conn)
    assert [r["ticker"] for r in rows] == ["PKN"]
    assert rows[0]["thesis"] == "Duży kontrakt." and rows[0]["telegram_message_id"] == 101
    parsed = list(csv.DictReader(io.StringIO(archive.to_csv(rows))))
    assert parsed[0]["ticker"] == "PKN" and list(parsed[0]) == archive.COLUMNS
    assert json.loads(archive.to_json(rows))[0]["stop_loss"] == 58.0
    assert archive.published_signals(db.conn, since=datetime(2026, 11, 3, tzinfo=UTC)) == []


def test_retention_disabled_by_default_and_keeps_open():
    db = _db_with_signals()
    now = datetime(2027, 1, 1, tzinfo=UTC)
    assert archive.prune(db.conn, None, now) == 0
    assert archive.prune(db.conn, 30, now) == 1     # tylko zamknięty PKN
    assert [r["ticker"] for r in db.all_signals()] == ["CDR"]


def test_cli_export_json(tmp_path, capsys):
    db_path = tmp_path / "s.sqlite"
    db = Database(db_path)
    sig = _signal()
    db.insert_signal(sig)
    db.mark_published(sig.id, 7)
    db.conn.close()
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(f"db_path: {db_path.as_posix()}\n", encoding="utf-8")
    out = tmp_path / "a.json"
    assert main(["--config", str(cfg), "export-signals", "--format", "json", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))[0]["ticker"] == "PKN"
    assert main(["--config", str(cfg), "prune-signals", "--yes"]) == 0
    assert "Retencja wyłączona" in capsys.readouterr().out
