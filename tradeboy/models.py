"""Plain data types shared across the desk."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


def pip_size(instrument: str) -> float:
    return 0.01 if instrument.endswith("_JPY") else 0.0001


def price_decimals(instrument: str) -> int:
    return 3 if instrument.endswith("_JPY") else 5


def currencies(instrument: str) -> tuple[str, str]:
    base, quote = instrument.split("_")
    return base, quote


@dataclass(frozen=True)
class Quote:
    instrument: str
    bid: float
    ask: float
    # Account-currency value of a 1.0 loss in the quote currency (OANDA homeConversions.accountLoss).
    quote_to_home_loss: float

    @property
    def spread_pips(self) -> float:
        return (self.ask - self.bid) / pip_size(self.instrument)


@dataclass(frozen=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class OpenTrade:
    trade_id: str
    instrument: str
    units: int  # negative for short
    entry_price: float
    stop_loss: float | None
    risk_amount: float  # account currency lost if the stop is hit


@dataclass(frozen=True)
class AccountState:
    # The executor replaces this with the desk's budget equity before any risk check.
    nav: float
    balance: float
    currency: str
    open_trades: list[OpenTrade]


@dataclass(frozen=True)
class TradeProposal:
    """What the head of desk hands to the executor."""

    instrument: str
    side: Side
    stop_loss: float
    take_profit: float | None = None
    rationale: str = ""
    debate_id: str | None = None


@dataclass(frozen=True)
class CalendarEvent:
    time: datetime
    currency: str
    title: str
    impact: str  # "high", "medium" or "low"


@dataclass
class RiskDecision:
    approved: bool
    units: int = 0
    risk_amount: float = 0.0
    reasons: list[str] = field(default_factory=list)
    kill_switch: bool = False
