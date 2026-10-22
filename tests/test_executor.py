from datetime import datetime, timezone

from tradeboy.config import RiskLimits, Settings
from tradeboy.executor import Executor
from tradeboy.journal import Journal, trading_day
from tradeboy.models import AccountState, OpenTrade, Quote, Side, TradeProposal

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
SETTINGS = Settings(pairs=["EUR_USD"], risk=RiskLimits())
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

    def market_order(self, instrument, units, stop_loss, take_profit=None):
        self.orders.append((instrument, units, stop_loss, take_profit))
        return {"orderFillTransaction": {"price": "1.1001", "tradeOpened": {"tradeID": "42"}}}

    def close_trade(self, trade_id):
        self.closed.append(trade_id)
        return {}


def test_dry_run_records_decision_but_places_no_order():
    broker, journal = FakeBroker(), Journal(":memory:")
    result = Executor(SETTINGS, broker, journal, dry_run=True).execute(PROPOSAL, NOW)
    assert result.decision.approved
    assert broker.orders == []
    assert journal.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_live_run_places_order_with_stop_and_journals_risk():
    broker, journal = FakeBroker(), Journal(":memory:")
    result = Executor(SETTINGS, broker, journal, dry_run=False).execute(PROPOSAL, NOW)
    assert broker.orders == [("EUR_USD", 74_074, 1.0951, 1.1101)]
    assert result.trade_id == "42"
    assert journal.risk_for("42") == result.decision.risk_amount


def test_rejected_trade_places_no_order():
    broker, journal = FakeBroker(), Journal(":memory:")
    bad = TradeProposal("EUR_USD", Side.BUY, stop_loss=1.2)
    result = Executor(SETTINGS, broker, journal, dry_run=False).execute(bad, NOW)
    assert not result.decision.approved
    assert broker.orders == []


def test_untracked_open_trades_count_at_full_budget():
    foreign = [OpenTrade("1", "GBP_USD", 1000, 1.3, 1.29, 0.0)]
    broker, journal = FakeBroker(trades=foreign), Journal(":memory:")
    account = Executor(SETTINGS, broker, journal)._account()
    assert account.open_trades[0].risk_amount == 500.0


def test_kill_switch_halts_and_closes_everything():
    broker, journal = FakeBroker(trades=[OpenTrade("7", "GBP_USD", 1000, 1.3, 1.29, 0.0)]), Journal(":memory:")
    executor = Executor(SETTINGS, broker, journal, dry_run=False)
    assert not executor.check_kill_switch(NOW)  # peak recorded at 100k
    broker.nav = 89_000
    assert executor.check_kill_switch(NOW)
    assert broker.closed == ["7"]
    assert journal.is_halted()
    assert not executor.execute(PROPOSAL, NOW).decision.approved


def test_trading_day_rolls_at_5pm_new_york():
    # 20:59 UTC = 16:59 New York (EDT): still the 22nd. 21:00 UTC = 17:00: the 23rd.
    assert trading_day(datetime(2026, 10, 22, 20, 59, tzinfo=timezone.utc)) == "2026-10-22"
    assert trading_day(datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)) == "2026-10-23"


def test_daily_loss_uses_first_mark_of_the_trading_day():
    broker, journal = FakeBroker(), Journal(":memory:")
    executor = Executor(SETTINGS, broker, journal, dry_run=True)
    executor.check_kill_switch(NOW)
    broker.nav = 97_900
    result = executor.execute(PROPOSAL, NOW)
    assert not result.decision.approved
    assert any("Daily loss" in r for r in result.decision.reasons)
