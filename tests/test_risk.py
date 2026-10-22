from datetime import datetime, timedelta, timezone

import pytest

from tradeboy.config import RiskLimits
from tradeboy.models import AccountState, CalendarEvent, OpenTrade, Quote, Side, TradeProposal
from dataclasses import replace

from tradeboy.risk import evaluate, kill_switch_reason, position_units

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
LIMITS = RiskLimits()
# EUR/USD at 1.1000/1.1001; 1 USD of loss costs 1.35 SGD.
QUOTE = Quote("EUR_USD", bid=1.1000, ask=1.1001, quote_to_home_loss=1.35)


def account(nav=100_000.0, trades=()):
    return AccountState(nav=nav, balance=nav, currency="SGD", open_trades=list(trades))


def trade(instrument="GBP_USD", risk=500.0, n=1):
    return OpenTrade(str(n), instrument, 1000, 1.3, 1.29, risk)


def run(proposal=None, acct=None, quote=QUOTE, peak=None, day_start=None, halted=False, events=()):
    acct = acct or account()
    return evaluate(
        LIMITS,
        proposal or TradeProposal("EUR_USD", Side.BUY, stop_loss=1.0951, take_profit=1.1101),
        acct,
        quote,
        peak_nav=peak or acct.nav,
        day_start_nav=day_start or acct.nav,
        halted=halted,
        events=list(events),
        now=NOW,
    )


def test_sizes_trade_to_half_percent_risk():
    d = run()
    assert d.approved
    # 500 SGD budget / (0.0050 stop distance * 1.35) = 74,074 units
    assert d.units == 74_074
    assert d.risk_amount == pytest.approx(500, abs=0.01)


def test_sell_units_are_negative():
    d = run(TradeProposal("EUR_USD", Side.SELL, stop_loss=1.1050))
    assert d.approved and d.units < 0


def test_position_units_zero_stop_distance():
    assert position_units(500, 1.1, 1.1, 1.35) == 0


@pytest.mark.parametrize(
    "proposal, message",
    [
        (TradeProposal("EUR_USD", Side.BUY, stop_loss=1.1010), "below the entry"),
        (TradeProposal("EUR_USD", Side.SELL, stop_loss=1.0990), "above the entry"),
        (TradeProposal("EUR_USD", Side.BUY, stop_loss=1.09, take_profit=1.0), "Take profit"),
    ],
)
def test_rejects_stops_and_targets_on_the_wrong_side(proposal, message):
    d = run(proposal)
    assert not d.approved
    assert any(message in r for r in d.reasons)


def test_rejects_when_max_open_trades_reached():
    d = run(acct=account(trades=[trade("GBP_USD", 100, 1), trade("USD_JPY", 100, 2), trade("AUD_USD", 100, 3)]))
    assert not d.approved
    assert any("open trades" in r for r in d.reasons)


def test_rejects_second_position_in_same_pair():
    d = run(acct=account(trades=[trade("EUR_USD", 100)]))
    assert not d.approved
    assert any("already open" in r for r in d.reasons)


def test_rejects_when_total_risk_cap_exceeded():
    # 2 trades at 800 = 1,600 open; a new 500 risk would make 2,100 > 2,000 cap.
    d = run(acct=account(trades=[trade("GBP_USD", 800, 1), trade("USD_JPY", 800, 2)]))
    assert not d.approved
    assert any("Total risk" in r for r in d.reasons)


def test_rejects_after_daily_loss_limit():
    d = run(acct=account(nav=97_900), day_start=100_000, peak=100_000)
    assert not d.approved
    assert any("Daily loss" in r for r in d.reasons)


def test_kill_switch_when_budget_is_used_up():
    d = run(acct=account(nav=-0.01), peak=100_000, day_start=1)
    assert not d.approved and d.kill_switch
    assert "budget is used up" in d.reasons[0]


def test_drawdown_kill_switch_is_off_by_default_and_optional():
    assert kill_switch_reason(LIMITS, 50, 100) is None
    assert "Drawdown" in kill_switch_reason(replace(LIMITS, max_drawdown_pct=10.0), 89.9, 100)


def test_halted_desk_rejects_everything():
    d = run(halted=True)
    assert not d.approved
    assert "halted" in d.reasons[0]


def test_rejects_wide_spread():
    d = run(quote=Quote("EUR_USD", bid=1.1000, ask=1.1005, quote_to_home_loss=1.35))
    assert not d.approved
    assert any("Spread" in r for r in d.reasons)


def test_jpy_pip_size_used_for_spread():
    jpy = Quote("USD_JPY", bid=150.000, ask=150.015, quote_to_home_loss=0.009)
    d = run(TradeProposal("USD_JPY", Side.BUY, stop_loss=149.5), quote=jpy)
    assert d.approved  # 1.5 pips


def test_news_blackout_for_either_currency():
    nfp = CalendarEvent(NOW + timedelta(minutes=10), "USD", "Non-Farm Payrolls", "high")
    d = run(events=[nfp])
    assert not d.approved
    assert any("Non-Farm Payrolls" in r for r in d.reasons)


def test_news_outside_window_or_low_impact_is_fine():
    events = [
        CalendarEvent(NOW + timedelta(minutes=30), "USD", "CPI", "high"),
        CalendarEvent(NOW + timedelta(minutes=5), "EUR", "Speech", "low"),
        CalendarEvent(NOW + timedelta(minutes=5), "JPY", "BoJ", "high"),
    ]
    assert run(events=events).approved
