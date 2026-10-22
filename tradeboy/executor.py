"""Turns the head of desk's decision into an order, but only after the hard risk limits pass."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol

from .config import Settings
from .journal import Journal
from .models import AccountState, CalendarEvent, Quote, RiskDecision, TradeProposal
from .risk import evaluate, kill_switch_reason


class Broker(Protocol):
    def account(self) -> AccountState: ...
    def quotes(self, instruments: list[str]) -> dict[str, Quote]: ...
    def to_home_rate(self, currency: str, via_instrument: str) -> float: ...
    def market_order(self, instrument: str, units: int, stop_loss: float, take_profit: float | None = None) -> dict: ...
    def close_trade(self, trade_id: str) -> dict: ...


class Calendar(Protocol):
    def events(self, now: datetime) -> list[CalendarEvent]: ...


class NoCalendar:
    """Placeholder until an economic calendar feed is chosen. News blackout is not enforced with it."""

    def events(self, now: datetime) -> list[CalendarEvent]:
        return []


@dataclass
class ExecutionResult:
    decision: RiskDecision
    decision_id: int
    trade_id: str | None = None
    fill_price: float | None = None


class Executor:
    def __init__(
        self, settings: Settings, broker: Broker, journal: Journal,
        calendar: Calendar | None = None, dry_run: bool = True,
    ):
        self.settings = settings
        self.broker = broker
        self.journal = journal
        self.calendar = calendar or NoCalendar()
        self.dry_run = dry_run

    def _start_budget(self, account: AccountState) -> tuple[float, float]:
        amount, currency = self.settings.starting_capital, self.settings.capital_currency
        rate = 1.0
        if currency != account.currency:
            via = next(p for p in self.settings.pairs if currency in p.split("_"))
            rate = self.broker.to_home_rate(currency, via)
        self.journal.start_budget(account.nav, amount * rate)
        return account.nav, amount * rate

    def _account(self) -> AccountState:
        """The account as the desk sees it: nav is the budget's equity, not the whole account."""
        account = self.broker.account()
        baseline, budget_home = self.journal.budget() or self._start_budget(account)
        equity = budget_home + account.nav - baseline
        per_trade = max(equity, 0) * self.settings.risk.risk_per_trade_pct / 100
        # Trades opened outside the journal are counted at a full per-trade budget, to stay conservative.
        trades = [
            replace(t, risk_amount=self.journal.risk_for(t.trade_id) or per_trade) for t in account.open_trades
        ]
        return replace(account, nav=equity, open_trades=trades)

    def usd_rate(self, account_currency: str) -> float:
        """Account-currency value of 1 USD, for comparing Claude API costs with trading results."""
        if account_currency == "USD":
            return 1.0
        return self.broker.to_home_rate("USD", next(p for p in self.settings.pairs if "USD" in p.split("_")))

    def budget_account(self) -> AccountState:
        return self._account()

    def check_kill_switch(self, now: datetime | None = None) -> bool:
        """Record equity and, if the budget is gone or drawdown hits its limit, close everything and halt."""
        now = now or datetime.now(timezone.utc)
        account = self._account()
        self.journal.mark_equity(now, account.nav)
        if self.journal.is_halted():
            return True
        if not kill_switch_reason(self.settings.risk, account.nav, self.journal.peak_nav() or account.nav):
            return False
        self.journal.set_halted(True)
        if not self.dry_run:
            for trade in account.open_trades:
                self.broker.close_trade(trade.trade_id)
        return True

    def execute(self, proposal: TradeProposal, now: datetime | None = None) -> ExecutionResult:
        now = now or datetime.now(timezone.utc)
        account = self._account()
        self.journal.mark_equity(now, account.nav)
        quote = self.broker.quotes([proposal.instrument])[proposal.instrument]

        decision = evaluate(
            self.settings.risk, proposal, account, quote,
            peak_nav=self.journal.peak_nav() or account.nav,
            day_start_nav=self.journal.day_start_nav(now) or account.nav,
            halted=self.journal.is_halted(),
            events=self.calendar.events(now),
            now=now,
        )
        decision_id = self.journal.record_decision(
            now, proposal.instrument, proposal.side.value, decision.approved, decision.units,
            decision.risk_amount, decision.reasons, proposal.rationale, proposal.debate_id, self.dry_run,
        )
        if decision.kill_switch:
            self.check_kill_switch(now)
        if not decision.approved or self.dry_run:
            return ExecutionResult(decision, decision_id)

        fill = self.broker.market_order(
            proposal.instrument, decision.units, proposal.stop_loss, proposal.take_profit
        )["orderFillTransaction"]
        trade_id = fill["tradeOpened"]["tradeID"]
        price = float(fill["price"])
        self.journal.record_trade(
            trade_id, decision_id, proposal.instrument, decision.units, price,
            proposal.stop_loss, proposal.take_profit, decision.risk_amount, now,
        )
        return ExecutionResult(decision, decision_id, trade_id, price)
