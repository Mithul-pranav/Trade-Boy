from datetime import date, timedelta

from fakes import FakeLLM, rebuttal, skeptic
from stock_fakes import (NOW, FakeAlpaca, FakeEarnings, FakeEdgar, FakeNews, far_earnings, filing, script,
                               settings)
from tradeboy.journal import Journal
from tradeboy.stocks.desk import StockDesk


class NoAlerts:
    def __init__(self):
        self.sent = []

    def send(self, kind, text, now):
        self.sent.append((kind, text))
        return True


def desk(broker=None, llm=None, edgar=None, earnings=None, news=None, dry_run=False, **kw):
    broker = broker or FakeAlpaca()
    return StockDesk(settings(**kw), broker, Journal(":memory:"), llm or FakeLLM(script()),
                     edgar=edgar if edgar is not None else FakeEdgar([filing()]), news=news or FakeNews(),
                     earnings=earnings or far_earnings(), dry_run=dry_run, alerts=NoAlerts())


def test_an_earnings_filing_is_debated_and_traded_with_a_stop_at_the_broker():
    d = desk()
    d.cycle(NOW)
    broker = d.broker
    entry, stop = broker.orders[0], broker.orders[1]
    assert entry["symbol"] == "NVDA" and entry["side"] == "buy" and entry["type"] == "market"
    assert stop["type"] == "stop" and stop["side"] == "sell" and stop["qty"] == entry["qty"]
    assert stop["time_in_force"] == "day"  # fractional shares: Alpaca only takes day stops
    assert "NVDA" in broker.held
    trade = d.journal.open_trades()[0]
    assert trade["exit_type"] == "target" and trade["hold_until"]
    assert any(k.startswith("opened:") for k, _ in d.alerts.sent)
    # The press release was read by the filings analyst.
    assert d.edgar.texts_read == ["0001-26-000001"]
    prompt = next(c["prompt"] for c in d.llm.calls if c["agent"] == "fundamentals")
    assert "Guidance raised" in prompt


def test_an_event_is_debated_only_once():
    d = desk()
    d.cycle(NOW)
    calls = len(d.llm.calls)
    d.broker.held.clear()  # even with no position left, the same filing isn't debated again
    d.cycle(NOW + timedelta(hours=21))
    assert not [c for c in d.llm.calls[calls:] if c["agent"] == "head_of_desk"]


def test_analysts_see_only_their_own_slice_first():
    d = desk(news=FakeNews({"NVDA": ["Nvidia beats"]}))
    d.cycle(NOW)
    first = {c["agent"]: c["prompt"] for c in d.llm.calls if c["agent"] in ("fundamentals", "news", "technical")}
    assert "Nvidia beats" in first["news"] and "Nvidia beats" not in first["technical"]
    assert "Guidance raised" not in first["news"]
    assert "independent views" not in first["technical"].lower()


def test_disagreement_triggers_rebuttals_and_unexplained_flips_are_ignored():
    s = script()
    s["news"] = {**s["news"], "stance": "short"}
    s["news_rebuttal"] = rebuttal("long", changed_by="")
    d = desk(llm=FakeLLM(s))
    d.cycle(NOW)
    agents = d.llm.agents_called()
    assert "news_rebuttal" in agents
    transcript = d.journal.db.execute("SELECT transcript FROM debates").fetchone()[0]
    assert "Stance change ignored" in transcript


def test_unanswered_serious_objection_halves_the_size():
    s = script()
    s["skeptic"] = skeptic(("O1", "serious"), ("O2", "fatal"))
    d = desk(llm=FakeLLM(s), dry_run=True)
    d.cycle(NOW)
    outcome = d.journal.db.execute("SELECT outcome FROM debates").fetchone()[0]
    assert "halved for unanswered objections O2" in outcome and "50% size" in outcome
    assert d.broker.orders == []  # dry run


def test_bad_stop_from_the_head_is_replaced_by_one_from_volatility():
    d = desk(llm=FakeLLM(script(stop=200.0)), dry_run=True)
    d.cycle(NOW)
    outcome = d.journal.db.execute("SELECT outcome FROM debates").fetchone()[0]
    assert "stop set from volatility" in outcome


def test_whole_share_short_too_big_for_the_budget_is_logged_not_placed():
    broker = FakeAlpaca(price=900.0)
    d = desk(broker=broker, llm=FakeLLM(script(price=900.0, side="short")))
    d.cycle(NOW)
    assert broker.orders == []
    outcome = d.journal.db.execute("SELECT outcome FROM debates").fetchone()[0]
    assert "short" in outcome and "whole shares" in outcome


def test_earnings_blackout_blocks_the_order_but_the_debate_still_ends_in_a_call():
    d = desk(earnings=FakeEarnings({"NVDA": date(2026, 10, 23)}))
    d.cycle(NOW)
    assert d.broker.orders == []
    outcome = d.journal.db.execute("SELECT outcome FROM debates").fetchone()[0]
    assert outcome.startswith("trade: long") and "Earnings on 2026-10-23" in outcome


def test_market_closed_does_nothing():
    d = desk(broker=FakeAlpaca(is_open=False))
    d.cycle(NOW)
    assert d.llm.calls == [] and d.broker.orders == []


def test_events_on_a_held_stock_go_to_the_exit_manager_not_a_new_debate():
    d = desk()
    d.cycle(NOW)
    d.edgar.list.append(filing(form="8-K", items=("5.02",), accession="0001-26-000002"))
    before = len(d.llm.calls)
    d.cycle(NOW + timedelta(minutes=10))
    new = [c["agent"] for c in d.llm.calls[before:]]
    assert "trade_reviewer" in new and "head_of_desk" not in new


def test_kill_switch_closes_everything_and_halts_when_the_budget_is_gone():
    d = desk()
    d.cycle(NOW)
    d.broker.equity_value -= 1000.0  # the whole budget lost
    d.cycle(NOW + timedelta(minutes=10))
    assert d.journal.is_halted() and "NVDA" in d.broker.closed
    assert any(k == "halted" for k, _ in d.alerts.sent)


def test_spend_cap_pauses_debates():
    d = desk(llm=FakeLLM(script(), cost_per_call=1.0))
    d.cycle(NOW)
    d.edgar.list.append(filing(symbol="MSFT", accession="0001-26-000009"))
    before = len(d.llm.calls)
    d.cycle(NOW + timedelta(minutes=10))
    assert len(d.llm.calls) == before
    assert any(k == "spend_cap" for k, _ in d.alerts.sent)


def test_calls_are_scored_in_basis_points():
    d = desk(dry_run=True)
    d.cycle(NOW)
    d.broker.later_price = 130.0
    d.broker.now = NOW + timedelta(days=10)
    d.score_calls(NOW + timedelta(days=10))
    records = d._track_records()
    assert records["head_of_desk"]["avg_bps"] > 400  # long from ~124 to 130
