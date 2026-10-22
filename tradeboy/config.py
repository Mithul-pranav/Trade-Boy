"""Loads settings.yaml and OANDA credentials from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: float = 0.5
    max_open_trades: int = 3
    max_total_risk_pct: float = 2.0
    daily_loss_limit_pct: float = 2.0
    max_drawdown_pct: float | None = None
    news_blackout_minutes: int = 15
    max_spread_pips: float = 3.0


@dataclass(frozen=True)
class Settings:
    pairs: list[str]
    risk: RiskLimits
    starting_capital: float = 100.0
    capital_currency: str = "USD"
    scan_interval_minutes: int = 15
    journal_path: Path = field(default=ROOT / "data" / "journal.sqlite3")


@dataclass(frozen=True)
class OandaCredentials:
    token: str
    account_id: str
    env: str = "practice"

    @property
    def base_url(self) -> str:
        if self.env == "live":
            return "https://api-fxtrade.oanda.com"
        return "https://api-fxpractice.oanda.com"


def load_settings(path: Path = DEFAULT_SETTINGS) -> Settings:
    raw = yaml.safe_load(path.read_text())
    journal_path = Path(raw.get("journal", {}).get("path", "data/journal.sqlite3"))
    if not journal_path.is_absolute():
        journal_path = ROOT / journal_path
    return Settings(
        pairs=list(raw["pairs"]),
        risk=RiskLimits(**raw.get("risk", {})),
        starting_capital=float(raw.get("capital", {}).get("starting_amount", 100)),
        capital_currency=raw.get("capital", {}).get("currency", "USD"),
        scan_interval_minutes=raw.get("scanner", {}).get("interval_minutes", 15),
        journal_path=journal_path,
    )


def load_credentials() -> OandaCredentials:
    token = os.environ.get("OANDA_API_TOKEN", "")
    account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
    if not token or not account_id:
        raise RuntimeError("Set OANDA_API_TOKEN and OANDA_ACCOUNT_ID in the environment.")
    env = os.environ.get("OANDA_ENV", "practice")
    if env not in ("practice", "live"):
        raise RuntimeError("OANDA_ENV must be 'practice' or 'live'.")
    return OandaCredentials(token=token, account_id=account_id, env=env)
