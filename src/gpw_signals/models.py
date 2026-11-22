"""Model domeny: news -> ocena agenta -> poziomy -> sygnał -> wynik."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SourceKind(StrEnum):
    ESPI = "espi"        # raporty bieżące/okresowe ESPI/EBI
    BROKER = "broker"    # rekomendacje biur maklerskich
    PORTAL = "portal"    # portale finansowe
    MACRO = "macro"      # RPP, GUS, kalendarz


@dataclass
class NewsItem:
    source: str
    kind: SourceKind
    title: str
    url: str
    published_at: datetime
    summary: str = ""
    priority: int = 1
    tickers: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        norm = re.sub(r"\W+", " ", self.title.lower()).strip()
        return hashlib.sha256(f"{self.url}|{norm}".encode()).hexdigest()[:32]


@dataclass
class Assessment:
    """Ocena agenta. Agent NIE podaje cen – tylko kierunek, siłę i uzasadnienie."""
    ticker: str
    actionable: bool
    direction: str            # "long" | "none"
    catalyst_type: str
    strength: float           # 0..1
    horizon_days: int
    already_priced_in: bool
    thesis: str
    risks: list[str] = field(default_factory=list)


@dataclass
class Levels:
    entry_min: float
    entry_max: float
    targets: list[float]      # [T1, T2, T3]
    stop_loss: float

    @property
    def entry_mid(self) -> float:
        return (self.entry_min + self.entry_max) / 2

    def pct(self, price: float) -> float:
        return (price / self.entry_mid - 1) * 100

    @property
    def risk(self) -> float:
        return self.entry_mid - self.stop_loss


class SignalStatus(StrEnum):
    PENDING_PUBLISH = "pending_publish"  # zapisany, jeszcze nie potwierdzony na kanale
    UNPUBLISHED = "unpublished"          # publikacja nie powiodła się w dopuszczalnym czasie
    ACTIVE = "active"
    T1 = "t1"
    T2 = "t2"
    T3 = "t3"
    STOPPED = "stopped"
    EXPIRED = "expired"


@dataclass
class Signal:
    ticker: str
    company_name: str
    levels: Levels
    score: float
    assessment: Assessment
    news_uid: str
    created_at: datetime        # aware, UTC (patrz clock.py)
    status: SignalStatus = SignalStatus.ACTIVE
    id: int | None = None
    telegram_message_id: int | None = None
