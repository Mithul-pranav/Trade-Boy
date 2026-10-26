"""Loads config/stocks.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import ROOT, ClaudeSettings

DEFAULT_STOCK_SETTINGS = ROOT / "config" / "stocks.yaml"


@dataclass(frozen=True)
class StockLimits:
    risk_per_trade_pct: float = 1.0
    max_open_positions: int = 4
    max_total_risk_pct: float = 4.0
    daily_loss_limit_pct: float = 3.0
    max_drawdown_pct: float | None = None
    max_position_pct: float = 35.0
    max_gross_pct: float = 100.0
    min_stop_pct: float = 1.0
    max_stop_pct: float = 12.0
    max_spread_pct: float = 0.25
    no_entry_first_minutes: int = 5
    no_entry_last_minutes: int = 15
    earnings_blackout_days: int = 2
    close_before_earnings: bool = True


@dataclass(frozen=True)
class StockClaude(ClaudeSettings):
    max_debates_per_day: int = 4
    max_events_per_scan: int = 1
    debate_cooldown_hours: float = 20.0
    web_searches_per_agent: int = 1


@dataclass(frozen=True)
class EventRules:
    filing_forms: tuple[str, ...] = ("8-K", "10-Q", "10-K")
    material_8k_items: tuple[str, ...] = ("1.01", "1.02", "2.01", "2.02", "2.05", "2.06", "4.02", "5.02", "7.01", "8.01")
    big_move_pct: float = 4.0
    big_move_sigma: float = 2.5
    news_burst_headlines: int = 8


@dataclass(frozen=True)
class StockSettings:
    universe: dict[str, dict]
    risk: StockLimits = field(default_factory=StockLimits)
    benchmark: str = "QQQ"
    starting_capital: float = 1000.0
    capital_currency: str = "USD"
    allow_shorts: bool = True
    scan_interval_minutes: int = 10
    claude: StockClaude = field(default_factory=StockClaude)
    events: EventRules = field(default_factory=EventRules)
    sec_user_agent: str = "Trade-Boy research"
    max_hold_days: int = 15
    team: dict = field(default_factory=dict)
    research: dict = field(default_factory=dict)
    journal_path: Path = field(default=ROOT / "data" / "stocks_journal.sqlite3")

    @property
    def symbols(self) -> list[str]:
        return list(self.universe)

    def name(self, symbol: str) -> str:
        return self.universe.get(symbol, {}).get("name", symbol)

    def group(self, symbol: str) -> str:
        return self.universe.get(symbol, {}).get("group", symbol)


def load_stock_settings(path: Path = DEFAULT_STOCK_SETTINGS) -> StockSettings:
    raw = yaml.safe_load(path.read_text())
    journal_path = Path(raw.get("journal", {}).get("path", "data/stocks_journal.sqlite3"))
    if not journal_path.is_absolute():
        journal_path = ROOT / journal_path
    events = raw.get("events") or {}
    return StockSettings(
        universe={s.upper(): dict(v or {}) for s, v in raw["universe"].items()},
        risk=StockLimits(**(raw.get("risk") or {})),
        benchmark=raw.get("benchmark", "QQQ"),
        starting_capital=float(raw.get("capital", {}).get("starting_amount", 1000)),
        capital_currency=raw.get("capital", {}).get("currency", "USD"),
        allow_shorts=bool(raw.get("allow_shorts", True)),
        scan_interval_minutes=int(raw.get("scanner", {}).get("interval_minutes", 10)),
        claude=StockClaude(**(raw.get("claude") or {})),
        events=EventRules(**{k: tuple(v) if isinstance(v, list) else v for k, v in events.items()}),
        sec_user_agent=(raw.get("sec") or {}).get("user_agent", "Trade-Boy research"),
        max_hold_days=int((raw.get("exits") or {}).get("max_hold_days", 15)),
        team=dict(raw.get("team") or {}),
        research=dict(raw.get("research") or {}),
        journal_path=journal_path,
    )
