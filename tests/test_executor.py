from datetime import datetime, timezone

import pytest

from tradeboy.config import RiskLimits, Settings
from tradeboy.executor import Executor
from tradeboy.journal import Journal, trading_day
from tradeboy.models import AccountState, OpenTrade, Quote, Side, TradeProposal

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
# A 100 USD budget inside a 100,000 SGD demo account; 1 USD = 1.30 SGD, so the budget is 130 SGD.
SETTINGS = Settings(pairs=["EUR_USD", "GBP_USD"], risk=RiskLimits(), starting_capital=100, capital_currency="USD")
PROPOSAL = TradeProposal("EUR_USD", Side.BUY, stop_loss=1.0951, take_profit=1.1101, rationale="test")


class FakeBroker:
    def __init__(self, nav=100_000.0, trades=()):
        self.nav = nav
        self.trades = list(trades)
        self.orders = []
        self.closed = []

    def account(self):
        return AccountState(self.nav, self.nav, "SGD", list(self.trades))

    def quotes(self, instruments):
        return {i: Quote(i, 1.1000, 1.1001, 1.35) for i in instruments}

    def to_home_rate(self, currency, via_instrument):
        assert currency == "USD" and "USD" in via_instrument
        return 1.30

    def market_order(self, instrument, units, stop_loss, take_profit=None):
        self.orders.append((instrument, units, stop_loss, take_profit))
        return {"orderFillTransaction": {"price": "1.1001", "tradeOpened": {"tradeID": "42"}}}

    def close_trade(self, trade_id):
        self.closed.append(trade_id)
        return {}


def make(broker=None, dry_run=False):
    broker, journal = broker or FakeBroker(), Journal(":memory:")
    return Executor(SETTINGS, broker, journal, dry_run=dry_run), broker, journal


def test_budget_starts_at_100_usd_in_account_currency():
    executor, broker, journal = make()
    assert executor._account().nav == pytest.approx(130.0)
    assert journal.budget() == (100_000.0, 130.0)
    broker.nav = 100_020.0  # profits stay in the budget
    assert executor._account().nav == pytest.approx(150.0)


def test_sizes_from_budget_not_whole_account():
    executor, broker, _ = make()
    result = executor.execute(PROPOSAL, NOW)
    # 0.5% of 130 SGD = 0.65 SGD risk; 0.65 / (0.0050 * 1.35) = 96 units
    assert broker.orders == [("EUR_USD", 96, 1.0951, 1.1101)]
    assert result.decision.risk_amount <= 0.65


def test_dry_run_records_decision_but_places_no_order():
    executor, broker, journal = make(dry_run=True)
    result = executor.execute(PROPOSAL, NOW)
    assert result.decision.approved
    assert broker.orders == []
    assert journal.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_live_run_journals_trade_risk():
    executor, _, journal = make()
    result = executor.execute(PROPOSAL, NOW)
    assert result.trade_id == "42"
    assert journal.risk_for("42") == result.decision.risk_amount


def test_rejected_trade_places_no_order():
    executor, broker, _ = make()
    result = executor.execute(TradeProposal("EUR_USD", Side.BUY, stop_loss=1.2), NOW)
    assert not result.decision.approved
    assert broker.orders == []


def test_untracked_open_trades_count_at_full_budget():
    executor, _, _ = make(FakeBroker(trades=[OpenTrade("1", "GBP_USD", 1000, 1.3, 1.29, 0.0)]))
    assert executor._account().open_trades[0].risk_amount == pytest.approx(0.65)


def test_losing_the_original_budget_halts_and_closes_everything():
    executor, broker, journal = make(FakeBroker(trades=[OpenTrade("7", "GBP_USD", 1000, 1.3, 1.29, 0.0)]))
    assert not executor.check_kill_switch(NOW)
    broker.nav = 99_875.0  # down 125 of the 130 SGD budget: still trading
    assert not executor.check_kill_switch(NOW)
    broker.nav = 99_870.0  # the whole budget is gone
    assert executor.check_kill_switch(NOW)
    assert broker.closed == ["7"]
    assert journal.is_halted()
    broker.nav = 100_500.0  # even a recovery doesn't restart it without permission
    assert not executor.execute(PROPOSAL, NOW).decision.approved


def test_profits_can_be_lost_without_halting():
    executor, broker, _ = make()
    executor.check_kill_switch(NOW)  # budget starts at 130
    broker.nav = 100_200.0  # budget grew to 330
    executor.check_kill_switch(NOW)
    broker.nav = 99_950.0  # gave back profits plus 50 of the original
    assert not executor.check_kill_switch(NOW)


def test_daily_loss_uses_first_mark_of_the_trading_day():
    executor, broker, _ = make(dry_run=True)
    executor.check_kill_switch(NOW)
    broker.nav = 99_997.0  # lost 3 SGD of a 130 budget today: 2.3%
    result = executor.execute(PROPOSAL, NOW)
    assert not result.decision.approved
    assert any("Daily loss" in r for r in result.decision.reasons)


def test_trading_day_rolls_at_5pm_new_york():
    # 20:59 UTC = 16:59 New York (EDT): still the 22nd. 21:00 UTC = 17:00: the 23rd.
    assert trading_day(datetime(2026, 10, 22, 20, 59, tzinfo=timezone.utc)) == "2026-10-22"
    assert trading_day(datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)) == "2026-10-23"
