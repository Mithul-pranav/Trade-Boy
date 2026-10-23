"""Plain data types for the stocks desk."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ..models import Side


@dataclass(frozen=True)
class StockQuote:
    symbol: str
    bid: float
    ask: float
    last: float
    time: datetime | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask >= self.bid else self.last

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid > 0 else 0.0


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float  # negative for short
    avg_entry: float
    current_price: float


@dataclass(frozen=True)
class Asset:
    symbol: str
    tradable: bool = True
    shortable: bool = True
    easy_to_borrow: bool = True
    fractionable: bool = True


@dataclass(frozen=True)
class Clock:
    now: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class Filing:
    symbol: str
    form: str
    accession: str
    filed: date
    accepted: datetime | None
    items: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class StockEvent:
    symbol: str
    kind: str  # "earnings", "filing", "big_move", "news_burst" or "manual"
    priority: int  # higher is debated first
    summary: str
    key: str  # unique, so the same event is never debated twice
    filing: Filing | None = None


@dataclass(frozen=True)
class StockProposal:
    """What the head of desk hands to the executor."""

    symbol: str
    side: Side
    stop_loss: float
    take_profit: float | None = None
    rationale: str = ""
    debate_id: str | None = None
    risk_fraction: float = 1.0
    hold_days: int = 5
    exit_type: str = "time_limit"
    exit_reason: str = ""


@dataclass
class StockDecision:
    approved: bool
    qty: float = 0.0  # signed: negative for a short
    risk_amount: float = 0.0
    reasons: list[str] | None = None
    kill_switch: bool = False

    def __post_init__(self):
        self.reasons = list(self.reasons or [])
