"""Monitoring: alerty na prywatny czat admina, znacznik życia dla healthchecka, raport dzienny, logi (#15)."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .clock import from_iso, to_iso
from .config import LoggingConfig

log = logging.getLogger(__name__)

_MAX_REASON = 300


@dataclass
class Monitor:
    """Liczy kolejne awarie każdego elementu. Jeden alert po przekroczeniu progu i jeden po powrocie."""
    notify: Callable[[str], object] | None = None   # None = tylko logi
    threshold: int = 3
    _fails: dict[str, int] = field(default_factory=dict)
    _alerted: set[str] = field(default_factory=set)

    def fail(self, what: str, reason: object, threshold: int | None = None) -> None:
        n = self._fails[what] = self._fails.get(what, 0) + 1
        if n >= (threshold or self.threshold) and what not in self._alerted:
            # Nieudana wysyłka: alert pójdzie przy następnej awarii
            if self.send(f"⚠️ {what}: awaria ({n} z rzędu)\n{str(reason)[:_MAX_REASON]}"):
                self._alerted.add(what)

    def ok(self, what: str) -> None:
        self._fails.pop(what, None)
        if what in self._alerted and self.send(f"✅ {what}: działa ponownie"):
            self._alerted.discard(what)

    def send(self, text: str) -> bool:
        """Błąd wysyłki nigdy nie przerywa pracy bota."""
        if self.notify is None:
            log.warning("Alert (brak czatu admina): %s", text)
            return True
        try:
            self.notify(text)
        except Exception as e:  # noqa: BLE001
            log.warning("Alert niewysłany (%s): %s", e, text)
            return False
        return True


# ------------------------------------------------------------------ znacznik życia

def write_heartbeat(path: str | Path, now: datetime) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(to_iso(now), encoding="utf-8")
    tmp.replace(p)


def heartbeat_problem(path: str | Path, now: datetime, max_age: timedelta) -> str | None:
    """None = bot żyje, inaczej opis problemu."""
    try:
        last = from_iso(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as e:
        return f"brak poprawnego znacznika: {e}"
    age = now - last
    return f"ostatni udany cykl {int(age.total_seconds() // 60)} min temu" if age > max_age else None


# ------------------------------------------------------------------ raport dzienny

def format_daily_report(day: str, report: dict | None, signals: int) -> str:
    """`report`: wpis z Database.daily_report dla tego dnia (None = brak danych)."""
    if report is None:
        return f"📋 Raport {day}\nBrak przetworzonych newsów. Sygnały: {signals}"
    stages = report["stages"]
    lines = [f"📋 Raport {day}",
             f"Nowe newsy: {stages.get('news:nowe', 0)}, wywołania LLM: {stages.get('llm', 0)}, sygnały: {signals}"]
    rejected = {k: n for k, n in stages.items() if k not in ("news:nowe", "llm", "sygnał")}
    if rejected:
        lines.append("Odrzucone lub pominięte:")
        lines += [f"  {k.partition(':')[2] or k}: {n}" for k, n in sorted(rejected.items(), key=lambda x: -x[1])]
    tokens = sum(m["input"] + m["output"] + m["cache_read"] + m["cache_write"] for m in report["models"].values())
    cost = "nieznany (model spoza cennika)" if report["cost_usd"] is None else f"{report['cost_usd']:.2f} USD"
    lines.append(f"Tokeny: {tokens}, koszt: {cost}")
    return "\n".join(lines)


# ------------------------------------------------------------------ logi

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
                "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging(cfg: LoggingConfig, verbose: bool = False, to_file: bool = False) -> None:
    """`to_file` tylko dla procesu `run`: rotacja pliku dzielonego przez dwa procesy zawodzi na Windows."""
    formatter = (JsonFormatter() if cfg.json_format
                 else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if to_file and cfg.file:
        Path(cfg.file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(cfg.file, maxBytes=int(cfg.max_mb * 1024 * 1024),
                                            backupCount=cfg.backups, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(formatter)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True)
    # httpx i httpcore logują pełny adres zapytania, a adres Bot API zawiera token Telegrama
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
