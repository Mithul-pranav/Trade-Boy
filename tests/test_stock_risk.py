from datetime import date, timedelta

from stock_fakes import NOW
from tradeboy.models import Side
from tradeboy.stocks.config import StockLimits
from tradeboy.stocks.models import Asset, Clock, StockProposal, StockQuote
from tradeboy.stocks.risk import evaluate, size, trading_days_until

OPEN = Clock(NOW, True, NOW + timedelta(days=1), NOW.replace(hour=20))


def check(proposal, price=100.0, nav=1000.0, positions=(), asset=None, clock=OPEN, earnings=None, shorts=True, **kw):
    quote = StockQuote(proposal.symbol, price - 0.01, price + 0.01, price)
    return evaluate(StockLimits(), proposal, nav=nav, open_positions=list(positions), quote=quote,
                    asset=asset or Asset(proposal.symbol), clock=clock, peak_nav=kw.get("peak", nav),
                    day_start_nav=kw.get("day_start", nav), halted=kw.get("halted", False), next_earnings=earnings,
                    allow_shorts=shorts, max_share=kw.get("max_share", 1.0))


def test_fractional_long_risks_one_percent_of_the_budget():
    d = check(StockProposal("NVDA", Side.BUY, stop_loss=95.0))
    assert d.approved
    assert abs(d.risk_amount - 10.0) < 0.01  # 1% of 1000
    assert d.qty != int(d.qty)  # fractional shares


def test_position_value_is_capped_so_a_gap_cannot_wipe_the_budget():
    d = check(StockProposal("NVDA", Side.BUY, stop_loss=98.9))  # tight stop: risk alone would buy ~9 shares
    assert d.approved and d.qty * 100.01 <= 350.01  # 35% of the budget


def test_short_must_be_whole_shares_and_says_why_when_one_share_is_too_much():
    d = check(StockProposal("MSFT", Side.SELL, stop_loss=530.0), price=500.0)
    assert not d.approved
    assert any("whole shares" in r for r in d.reasons)
    ok = check(StockProposal("INTC", Side.SELL, stop_loss=42.0), price=40.0)
    assert ok.approved and ok.qty == -4.0  # 10 USD / 2.01 per share, rounded down


def test_shorts_switched_off_are_logged_not_placed():
    d = check(StockProposal("NVDA", Side.SELL, stop_loss=105.0), shorts=False)
    assert not d.approved and any("Shorts are switched off" in r for r in d.reasons)


def test_hard_to_borrow_cannot_be_shorted():
    d = check(StockProposal("NVDA", Side.SELL, stop_loss=105.0), asset=Asset("NVDA", easy_to_borrow=False))
    assert not d.approved


def test_earnings_blackout():
    d = check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), earnings=date(2026, 10, 26))  # Monday: 2 trading days
    assert not d.approved and any("Earnings" in r for r in d.reasons)
    assert check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), earnings=date(2026, 10, 30)).approved


def test_no_entries_near_the_open_or_close_or_when_closed():
    near_close = Clock(NOW.replace(hour=19, minute=50), True, NOW, NOW.replace(hour=20))
    assert not check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), clock=near_close).approved
    at_open = Clock(NOW.replace(hour=13, minute=32), True, NOW, NOW.replace(hour=20))
    assert not check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), clock=at_open).approved
    closed = Clock(NOW, False, NOW, NOW)
    assert not check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), clock=closed).approved


def test_stop_band_and_side():
    assert not check(StockProposal("NVDA", Side.BUY, stop_loss=99.5)).approved  # under 1%
    assert not check(StockProposal("NVDA", Side.BUY, stop_loss=80.0)).approved  # over 12%
    assert not check(StockProposal("NVDA", Side.BUY, stop_loss=101.0)).approved


def test_total_risk_gross_and_count_limits():
    open_ = [{"symbol": s, "qty": 1, "value": 200.0, "risk": 10.0} for s in ("A", "B", "C", "D")]
    d = check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), positions=open_)
    assert not d.approved and any("open positions" in r for r in d.reasons)
    full = [{"symbol": "A", "qty": 1, "value": 1000.0, "risk": 5.0}]
    assert not check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), positions=full).approved  # no leverage
    risky = [{"symbol": "A", "qty": 1, "value": 100.0, "risk": 35.0}]
    assert not check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), positions=risky).approved


def test_kill_switch_and_daily_loss():
    d = check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), nav=0.0)
    assert d.kill_switch
    d = check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), nav=960.0, day_start=1000.0)
    assert not d.approved and any("Daily loss" in r for r in d.reasons)


def test_risk_quant_limit_shrinks_size():
    full = check(StockProposal("NVDA", Side.BUY, stop_loss=95.0))
    half = check(StockProposal("NVDA", Side.BUY, stop_loss=95.0), max_share=0.5)
    assert abs(half.risk_amount - full.risk_amount / 2) < 0.01


def test_helpers():
    assert size(10, 100, 95, 1000, fractional=False) == 2.0
    assert size(10, 100, 95, 1000, fractional=True) == 2.0
    assert size(10, 100, 97, 1000, fractional=True) == 3.3333
    assert trading_days_until(date(2026, 10, 23), date(2026, 10, 26)) == 1  # Friday to Monday
