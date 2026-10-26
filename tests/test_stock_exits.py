from datetime import date, timedelta

from fakes import FakeLLM
from stock_fakes import NOW, FakeAlpaca, FakeEarnings, FakeEdgar, FakeNews, far_earnings, filing, script, settings
from test_stock_desk import NoAlerts
from tradeboy.journal import Journal
from tradeboy.stocks.desk import StockDesk


def opened(earnings=None, **script_kw):
    """A desk that has just opened an NVDA long from an earnings filing."""
    d = StockDesk(settings(), FakeAlpaca(), Journal(":memory:"), FakeLLM(script(**script_kw)), edgar=FakeEdgar([filing()]),
                  news=FakeNews(), earnings=earnings or far_earnings(), dry_run=False, alerts=NoAlerts())
    d.cycle(NOW)
    assert "NVDA" in d.broker.held
    return d


def manage(d, now):
    d.broker.now = now
    d.positions.manage(now, can_spend=lambda: True)


def test_time_limit_closes_the_position():
    d = opened()
    manage(d, NOW + timedelta(days=8))
    assert d.broker.closed == ["NVDA"]
    manage(d, NOW + timedelta(days=8, minutes=10))
    trade = d.journal.db.execute("SELECT close_note, realized_pl FROM trades").fetchone()
    assert "time limit" in trade[0] and trade[1] is not None


def test_closed_before_earnings_whatever_the_plan():
    earnings = far_earnings()
    d = opened(earnings)
    earnings.dates["NVDA"] = date(2026, 10, 23)
    manage(d, NOW + timedelta(minutes=10))
    assert d.broker.closed == ["NVDA"]


def test_target_is_watched_in_code():
    d = opened()
    target = d.journal.open_trades()[0]["take_profit"]
    d.broker.price["NVDA"] = target + 1
    manage(d, NOW + timedelta(minutes=10))
    assert d.broker.closed == ["NVDA"]


def test_missing_stop_is_placed_again_next_session():
    d = opened()
    stop = d.broker.open_orders("NVDA")[0]
    stop["status"] = "expired"  # fractional stops are day orders
    manage(d, NOW + timedelta(days=1))
    new = d.broker.open_orders("NVDA")
    assert len(new) == 1 and new[0]["type"] == "stop" and new[0]["stop_price"] == stop["stop_price"]


def test_trailing_stop_follows_one_r_behind_and_survives_a_new_session():
    d = opened(exit_type="trailing_stop", target=None)
    trade = d.journal.open_trades()[0]
    risk = trade["entry_price"] - trade["stop_loss"]
    d.broker.price["NVDA"] = trade["entry_price"] + 1.5 * risk
    manage(d, NOW + timedelta(minutes=10))
    assert d.broker.replaced and abs(d.broker.replaced[-1][1] - (d.broker.price["NVDA"] - 0.01 - risk)) < 0.02
    level = d.broker.replaced[-1][1]
    d.broker.open_orders("NVDA")[0]["status"] = "expired"
    manage(d, NOW + timedelta(days=1))
    assert d.broker.open_orders("NVDA")[0]["stop_price"] == level  # not back to the entry stop


def test_stop_filled_at_the_broker_is_recorded_with_its_result():
    d = opened()
    trade = d.journal.open_trades()[0]
    d.broker.stop_hit("NVDA", trade["stop_loss"])
    manage(d, NOW + timedelta(hours=1))
    pl = d.journal.db.execute("SELECT realized_pl FROM trades").fetchone()[0]
    assert pl < 0 and abs(pl + trade["units"] * (trade["entry_price"] - trade["stop_loss"])) < 0.01
    assert any(k.startswith("closed:") for k, _ in d.alerts.sent)


def test_sharp_move_against_triggers_one_review_that_can_close():
    s = script()
    s["trade_reviewer"] = {"action": "close", "reason": "thesis broken"}
    d = opened()
    d.llm.script["trade_reviewer"] = s["trade_reviewer"]
    trade = d.journal.open_trades()[0]
    d.broker.price["NVDA"] = trade["entry_price"] - 0.6 * (trade["entry_price"] - trade["stop_loss"])
    manage(d, NOW + timedelta(minutes=10))
    assert d.llm.agents_called().count("trade_reviewer") == 1 and d.broker.closed == ["NVDA"]
