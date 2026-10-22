from datetime import datetime, timedelta, timezone

from fakes import FakeBroker, FakeLLM, agreeing_script, head

from tradeboy.config import load_settings
from tradeboy.desk import Desk
from tradeboy.journal import Journal
from tradeboy.models import CalendarEvent, OpenTrade, Quote

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)  # a Thursday
SETTINGS = load_settings()


class RecordingAlerts:
    def __init__(self):
        self.sent = []

    def send(self, kind, text, now):
        self.sent.append((kind, text))
        return True


class PricedBroker(FakeBroker):
    """Fills at 1.1001 like FakeBroker, then quotes whatever price the test sets."""

    def __init__(self):
        super().__init__()
        self.bid = 1.1000

    def quotes(self, instruments):
        return {i: Quote(i, self.bid, self.bid + 0.0001, 1.35) for i in instruments}

    def market_order(self, instrument, units, stop_loss, take_profit=None):
        result = super().market_order(instrument, units, stop_loss, take_profit)
        self.trades.append(OpenTrade("42", instrument, units, 1.1001, stop_loss, 0.0))
        return result


class Calendar:
    def __init__(self, events=()):
        self.list = list(events)

    def events(self, now):
        return self.list

    def upcoming(self, currencies, now, hours=48):
        return [e for e in self.list if e.currency in currencies and now <= e.time <= now + timedelta(hours=hours)]


def opened(exit_type="target", calendar=None, review="hold"):
    script = agreeing_script()
    script["head_of_desk"] = head(stop=1.0951, answers=[("O1", True)], exit_type=exit_type)
    script["trade_reviewer"] = {"action": review, "reason": "because"}
    broker = PricedBroker()
    desk = Desk(SETTINGS, broker, Journal(":memory:"), FakeLLM(script), calendar, dry_run=False)
    desk.alerts = desk.trades.alerts = RecordingAlerts()
    desk.debate("EUR_USD", NOW)
    return desk, broker


def test_exit_plan_is_written_at_entry_and_reported():
    desk, _ = opened()
    trade = desk.journal.open_trades()[0]
    assert trade["exit_type"] == "target" and "Close early if: c" in trade["exit_reason"]
    assert trade["hold_until"] == (NOW + timedelta(hours=24)).isoformat()
    assert desk.alerts.sent[0][0] == "opened:42" and "target" in desk.alerts.sent[0][1]


def test_time_limit_closes_the_trade():
    desk, broker = opened()
    desk.trades.manage(NOW + timedelta(hours=23), can_spend=lambda: True)
    assert broker.closed == []
    desk.trades.manage(NOW + timedelta(hours=24), can_spend=lambda: True)
    assert broker.closed == ["42"]
    assert desk.journal.open_trades() == []
    assert "time limit" in desk.alerts.sent[-1][1]


def test_trailing_stop_follows_price_after_one_r():
    desk, broker = opened("trailing_stop")
    broker.bid = 1.1030  # +0.6R: not yet
    desk.trades.manage(NOW, can_spend=lambda: True)
    assert broker.stops == []
    broker.bid = 1.1101  # +2R: the stop moves to 1R behind price
    desk.trades.manage(NOW, can_spend=lambda: True)
    assert broker.stops == [("42", 1.1051)]
    broker.bid = 1.1090  # price falls back: the stop never moves down
    desk.trades.manage(NOW, can_spend=lambda: True)
    assert len(broker.stops) == 1


def test_target_trades_do_not_trail():
    desk, broker = opened("target")
    broker.bid = 1.1101
    desk.trades.manage(NOW, can_spend=lambda: True)
    assert broker.stops == []


def test_sharp_move_against_asks_for_one_review_and_can_close():
    desk, broker = opened(review="close")
    broker.bid = 1.0970  # 31 of 50 pips towards the stop
    desk.trades.manage(NOW, can_spend=lambda: True)
    assert desk.llm.agents_called().count("trade_reviewer") == 1
    assert broker.closed == ["42"]
    assert "re-review" in desk.alerts.sent[-1][1]


def test_review_happens_once_per_trigger_and_not_without_budget():
    desk, broker = opened(review="hold")
    broker.bid = 1.0970
    desk.trades.manage(NOW, can_spend=lambda: False)
    assert "trade_reviewer" not in desk.llm.agents_called()
    desk.trades.manage(NOW, can_spend=lambda: True)
    desk.trades.manage(NOW, can_spend=lambda: True)
    assert desk.llm.agents_called().count("trade_reviewer") == 1
    assert broker.closed == []


def test_high_impact_event_triggers_review():
    calendar = Calendar([CalendarEvent(NOW + timedelta(minutes=40), "USD", "Non-Farm Payrolls", "high")])
    desk, broker = opened(calendar=calendar)
    desk.trades.manage(NOW, can_spend=lambda: True)
    assert desk.llm.agents_called().count("trade_reviewer") == 1
    assert "Non-Farm Payrolls" in desk.llm.calls[-1]["prompt"]


def test_stop_hit_at_oanda_is_recorded_and_reported():
    desk, broker = opened()
    broker.trades = []
    broker.realized["42"] = -6.4
    desk.trades.manage(NOW, can_spend=lambda: True)
    assert desk.journal.open_trades() == []
    assert desk.alerts.sent[-1][0] == "closed:42" and "-6.40" in desk.alerts.sent[-1][1]


def test_old_journal_gets_the_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE trades (trade_id TEXT PRIMARY KEY, decision_id INTEGER, instrument TEXT NOT NULL,"
               " units INTEGER NOT NULL, entry_price REAL NOT NULL, stop_loss REAL NOT NULL, take_profit REAL,"
               " risk_amount REAL NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, realized_pl REAL)")
    db.commit()
    db.close()
    journal = Journal(path)
    journal.record_trade("1", 1, "EUR_USD", 10, 1.1, 1.09, None, 1.0, NOW)
    assert journal.open_trades()[0]["exit_type"] == "time_limit"
