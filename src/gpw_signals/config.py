"""Konfiguracja: progi i parametry z settings.yaml, sekrety z .env / zmiennych środowiskowych."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_channel_id: str = ""
    telegram_test_channel_id: str = ""
    telegram_admin_chat_id: str = ""   # prywatny czat na alerty i raport dzienny


class FeedConfig(BaseModel):
    name: str
    kind: str  # espi | portal | broker | macro
    url: str
    priority: int = 1


class SourcesConfig(BaseModel):
    rss: list[FeedConfig] = Field(default_factory=list)
    broker_keywords: list[str] = Field(default_factory=list)
    macro_calendar_file: str = "data/macro_calendar.yaml"
    fetch_report_schedules: bool = False   # pobieranie treści komunikatów z terminami raportów okresowych
    fetch_espi_body: bool = False          # pobieranie treści komunikatu ESPI, gdy RSS ma tylko tytuł (#7)
    max_body_fetches: int = 10             # limit pobrań stron komunikatów na cykl
    report_calendar_file: str = "data/report_calendar.yaml"


class ScheduleConfig(BaseModel):
    poll_minutes: int = 5
    active_from: str = "08:00"
    active_to: str = "17:10"
    prices_at: str = "18:15"     # import dziennego pliku z archiwum GPW (po publikacji)
    tracker_at: str = "18:30"   # po imporcie notowań
    holidays: list[str] = Field(default_factory=list)
    holidays_file: str | None = None   # YAML: rok -> lista dat; dokładane do `holidays` w load_settings


class MarketConfig(BaseModel):
    provider: str = "stooq"
    stooq_url: str = "https://stooq.pl/q/d/l/?s={symbol}&i=d"
    csv_dir: str = "data/prices"
    # Archiwum GPW (darmowe dane dzienne) – import do csv_dir
    gpw_archive_url: str = "https://www.gpw.pl/archiwum-notowan?fetch=1&type={type}&instrument=&date={date}"
    raw_dir: str = "data/raw/gpw"
    min_interval_s: float = 1.5
    index_symbols: dict[str, str] = Field(default_factory=lambda: {"WIG20": "wig20", "WIG": "wig"})
    # Kontrola jakości danych przed liczeniem poziomów
    max_data_age_days: int = 5
    max_jump_pct: float = 40.0
    history_days: int = 250
    index_symbol: str = "wig20"
    # Bieżący kurs w sesji (#4): none = tylko świece dzienne; csv = {intraday_dir}/{symbol}.csv
    intraday_provider: str = "none"
    intraday_dir: str = "data/intraday"
    intraday_max_age_min: float = 20.0   # starszy kurs = fallback do świec dziennych
    session_open: str = "09:00"
    session_close: str = "17:05"


class AgentConfig(BaseModel):
    provider: Literal["anthropic", "ollama"] = "anthropic"
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_tokens: int = 16000
    max_tool_rounds: int = 8
    use_fallbacks: bool = True
    # Tylko provider: ollama
    base_url: str = "http://localhost:11434"
    num_ctx: int = 8192
    timeout_s: float = 300.0

    @model_validator(mode="after")
    def _local_model_name(self) -> AgentConfig:
        if self.provider == "ollama" and self.model.startswith("claude"):
            raise ValueError("provider: ollama wymaga nazwy lokalnego modelu w agent.model")
        return self


class LevelsConfig(BaseModel):
    atr_period: int = 14
    entry_below_atr: float = 0.3
    entry_above_atr: float = 0.2
    sl_atr: float = 1.5
    sl_swing_buffer_atr: float = 0.2
    max_sl_pct: float = 8.0
    r_multiples: list[float] = Field(default_factory=lambda: [1.0, 2.0, 3.0])
    resistance_snap_r: float = 0.5
    swing_window: int = 5
    swing_lookback: int = 120


class FiltersConfig(BaseModel):
    min_avg_turnover_pln: float = 1_000_000
    min_rr_t1: float = 1.0
    require_above_sma50: bool = True
    strong_catalyst_override: float = 0.8
    min_volume_ratio: float = 1.2
    max_gap_pct: float = 6.0
    block_bear_regime: bool = True
    cooldown_days: int = 5
    max_signals_per_day: int = 5
    min_score: float = 0.55


class ProcessingConfig(BaseModel):
    max_news_age_hours: float = 12.0   # starsze nieprzetworzone newsy uznajemy za nieaktualne
    max_attempts: int = 3              # ile razy ponawiać news po błędzie przejściowym


class PrefilterConfig(BaseModel):
    enabled: bool = True
    min_insider_value_pln: float = 200_000  # mniejsze transakcje osób blisko związanych = szum


class PublishConfig(BaseModel):
    enabled: bool = True
    max_pending_minutes: int = 60      # po tym czasie nieopublikowany sygnał nie jest już wysyłany
    use_test_channel: bool = True
    include_thesis: bool = True
    post_outcomes: bool = True
    disclaimer: str = ""
    methodology_url: str = ""          # link do opisu metodologii (docs/METODOLOGIA.md) w każdym sygnale
    # Konflikty interesów (#16): spółki, w których autor ma pozycję – sygnał dostaje dopisek
    conflict_tickers: list[str] = Field(default_factory=list)
    conflict_note: str = "Ujawnienie: autor kanału posiada pozycję w akcjach tej spółki."


class ArchiveConfig(BaseModel):
    # None = archiwum opublikowanych sygnałów nie jest nigdy usuwane (domyślnie)
    retention_days: int | None = None


class TrackerConfig(BaseModel):
    breakeven_after_t1: bool = False   # po T1 stop na środek strefy wejścia (od następnej sesji)
    weekly_summary: bool = False       # podsumowanie skuteczności na kanale
    weekly_summary_day: str = "fri"
    weekly_summary_at: str = "19:00"


class MonitoringConfig(BaseModel):
    fail_threshold: int = 3            # alert po tylu awariach z rzędu
    heartbeat_file: str = "data/heartbeat"
    max_heartbeat_age_min: int = 30    # starszy znacznik = kontener "unhealthy"
    daily_report: bool = True
    daily_report_at: str = "19:15"     # po trackerze


class LoggingConfig(BaseModel):
    json_format: bool = False
    file: str = ""                     # "" = tylko konsola; plik zapisuje wyłącznie `run`
    max_mb: float = 10.0
    backups: int = 5


class Settings(BaseModel):
    db_path: str = "data/gpw_signals.sqlite"
    tickers_csv: str = "data/gpw_tickers.csv"
    timezone: str = "Europe/Warsaw"
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    levels: LevelsConfig = Field(default_factory=LevelsConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    secrets: Secrets = Field(default_factory=Secrets)


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    s = Settings(**(data or {}), secrets=Secrets())
    if s.schedule.holidays_file:
        s.schedule.holidays = sorted(set(s.schedule.holidays) | set(load_holidays(s.schedule.holidays_file)))
    return s


def load_holidays(path: str | Path) -> list[str]:
    """Dni bez sesji z pliku YAML ({rok: [YYYY-MM-DD, ...]}). Brak pliku to błąd: bot handlowałby w święta."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = []
    for year, days in data.items():
        for d in days or []:
            iso = date.fromisoformat(str(d)).isoformat()
            if not iso.startswith(f"{year}-"):
                raise ValueError(f"{path}: {iso} wpisany pod rokiem {year}")
            out.append(iso)
    return out
