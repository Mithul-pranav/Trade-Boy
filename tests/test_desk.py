from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fakes import FakeBroker, FakeLLM, agreeing_script, head, skeptic

from tradeboy.config import load_settings
from tradeboy.desk import Desk
from tradeboy.journal import Journal
from tradeboy.market import find_setups, market_open

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)  # a Thursday
SETTINGS = load_settings()


def make(script=None, dry_run=True, settings=SETTINGS, broker=None, cost=0.01):
    broker = broker or FakeBroker()
    llm = FakeLLM(script or agreeing_script(), cost_per_call=cost)
    return Desk(settings, broker, Journal(":memory:"), llm, dry_run=dry_run), broker, llm


def test_dry_run_debate_is_journaled_but_places_no_order():
    desk, broker, _ = make()
    result = desk.debate("EUR_USD", NOW)
    assert result.outcome.startswith("trade: long") and "dry run, would open" in result.outcome
    assert broker.orders == []
    row = desk.journal.db.execute("SELECT instrument, analysts_agreed_first FROM debates").fetchone()
    assert row == ("EUR_USD", 1)
    assert desk.journal.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_paper_debate_places_order_sized_by_risk_manager():
    desk, broker, _ = make(dry_run=False)
    desk.debate("EUR_USD", NOW)
    # The risk manager allowed half size for a long: 0.325 SGD / (0.0051 * 1.35) = 47 units.
    assert broker.orders[0][1] == 47


def test_too_tight_stop_is_widened_so_the_order_still_goes_in():
    script = agreeing_script()
    script["head_of_desk"] = head(stop=1.0999, answers=[("O1", True)])  # a 2-pip stop
    desk, broker, _ = make(script, dry_run=False)
    result = desk.debate("EUR_USD", NOW)
    assert "stop widened" in result.outcome
    entry = broker.quotes(["EUR_USD"])["EUR_USD"].ask
    assert round((entry - broker.orders[0][2]) / 0.0001, 1) == SETTINGS.risk.min_stop_pips + 1


def test_hard_limits_still_apply_after_the_debate():
    settings = replace(SETTINGS, risk=replace(SETTINGS.risk, max_open_trades=0))
    desk, broker, _ = make(settings=settings, dry_run=False)
    result = desk.debate("EUR_USD", NOW)
    assert broker.orders == []
    assert "hard limits rejected" in result.outcome


def test_api_usage_is_recorded_and_the_spend_cap_stops_debates():
    settings = replace(SETTINGS, claude=replace(SETTINGS.claude, daily_budget_usd=0.05))
    desk, _, llm = make(settings=settings, cost=0.01)
    assert desk.debate("EUR_USD", NOW) is not None
    assert desk.journal.day_spend(NOW) > 0.05
    calls = len(llm.calls)
    assert desk.debate("GBP_USD", NOW) is None
    assert len(llm.calls) == calls


def test_halted_desk_does_not_debate():
    desk, _, llm = make()
    desk.journal.set_halted(True)
    desk.cycle(NOW)
    assert desk.debate("EUR_USD", NOW) is None
    assert llm.calls == []


def test_cycle_does_nothing_while_market_is_closed():
    desk, broker, llm = make()
    desk.cycle(datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc))  # Saturday
    assert llm.calls == [] and broker.candle_calls == 0


def test_scan_skips_claude_when_screen_finds_few_pairs(monkeypatch):
    import tradeboy.desk as desk_module
    monkeypatch.setattr(desk_module, "find_setups", lambda h1, h4: ["test setup"] if h1 else [])
    settings = replace(SETTINGS, pairs=["EUR_USD"])
    desk, _, llm = make(settings=settings)
    assert desk.scan(NOW) == [("EUR_USD", "test setup")]
    assert llm.calls == []


def test_scan_asks_claude_to_choose_between_many(monkeypatch):
    import tradeboy.desk as desk_module
    monkeypatch.setattr(desk_module, "find_setups", lambda h1, h4: ["setup"])
    script = {"scanner": {"flagged": [{"instrument": "USD_JPY", "reason": "cleanest"},
                                      {"instrument": "NOT_A_PAIR", "reason": "made up"}]}}
    desk, _, llm = make(script)
    assert desk.scan(NOW) == [("USD_JPY", "cleanest")]
    assert llm.calls[0]["model"] == SETTINGS.claude.scanner_model


def test_recently_debated_pair_is_skipped(monkeypatch):
    import tradeboy.desk as desk_module
    monkeypatch.setattr(desk_module, "find_setups", lambda h1, h4: ["setup"])
    settings = replace(SETTINGS, pairs=["EUR_USD"])
    desk, _, _ = make(settings=settings)
    desk.debate("EUR_USD", NOW)
    assert desk.scan(NOW + timedelta(hours=1)) == []
    assert desk.scan(NOW + timedelta(hours=5)) == [("EUR_USD", "setup")]


def test_daily_debate_limit():
    desk, _, _ = make()
    for pair in ["EUR_USD", "GBP_USD", "USD_JPY"]:
        desk.debate(pair, NOW)
    calls = []
    desk.scan = lambda now: calls.append(now) or []
    desk.cycle(NOW)
    assert calls == []


def test_calls_are_scored_after_their_horizon():
    desk, broker, _ = make()
    desk.debate("EUR_USD", NOW)
    broker.later_price = 1.1030  # 30 pips up from the 1.10005 mid
    desk.score_calls(NOW + timedelta(hours=1))
    assert desk.journal.track_records() == {}
    desk.score_calls(NOW + timedelta(hours=25))
    records = desk.journal.track_records()
    assert records["technical"]["hit_rate_pct"] == 100
    assert records["technical"]["avg_pips"] == 29.5


def test_track_records_reach_the_head_of_desk():
    desk, broker, llm = make()
    desk.debate("EUR_USD", NOW)
    broker.later_price = 1.0970
    desk.score_calls(NOW + timedelta(hours=25))
    desk.debate("GBP_USD", NOW + timedelta(hours=25))
    prompt = [c for c in llm.calls if c["agent"] == "head_of_desk"][-1]["prompt"]
    assert '"hit_rate_pct": 0' in prompt


def test_agreement_alarm_after_many_unanimous_debates():
    desk, _, _ = make(settings=replace(SETTINGS, claude=replace(SETTINGS.claude, monthly_budget_usd=100,
                                                                daily_budget_usd=100)))
    for i in range(10):
        desk.debate("EUR_USD", NOW + timedelta(minutes=i))
    assert desk.journal.get_state("agreement_alarm").startswith("1.00 over 10")


def test_failed_agent_call_does_not_crash_the_desk():
    from tradeboy.llm import AgentError

    def boom():
        raise AgentError("bad json")

    script = agreeing_script()
    script["skeptic"] = boom
    desk, _, _ = make(script)
    assert desk.debate("EUR_USD", NOW) is None
    assert desk.journal.month_spend(NOW) > 0  # the calls that did run are still counted


def test_market_hours():
    assert market_open(NOW)
    assert not market_open(datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc))  # Saturday
    assert not market_open(datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc))  # Sunday 16:00 NY
    assert market_open(datetime(2026, 10, 25, 21, 30, tzinfo=timezone.utc))  # Sunday 17:30 NY
    assert not market_open(datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc))  # Friday 17:30 NY


def summary(trend="mixed", rsi=50, high=20, low=20, atr=10, change=0):
    return {"trend": trend, "rsi14": rsi, "pips_from_range_high": high, "pips_from_range_low": low,
            "atr14_pips": atr, "change_over_window_pips": change}


def test_code_screen():
    assert find_setups(summary(), summary()) == []
    assert "pullback" in find_setups(summary(rsi=40), summary(trend="up"))[0]
    assert "bounce" in find_setups(summary(rsi=60), summary(trend="down"))[0]
    assert "top of a 4h range" in find_setups(summary(), summary(high=2, low=40))[0]
    assert "breakout" in find_setups(summary(change=50, high=1), summary(trend="up"))[0]


class RecordingAlerts:
    def __init__(self):
        self.sent = []

    def send(self, kind, text, now):
        self.sent.append((kind, text))
        return True


def test_out_of_credits_pauses_agents_and_alerts_mithul():
    from tradeboy.llm import CreditsExhausted

    def no_credits():
        raise CreditsExhausted("credit balance is too low")

    script = agreeing_script()
    script["technical"] = no_credits
    desk, _, llm = make(script)
    desk.alerts = alerts = RecordingAlerts()
    assert desk.debate("EUR_USD", NOW) is None
    assert alerts.sent[0][0] == "credits"
    calls = len(llm.calls)
    assert desk.debate("EUR_USD", NOW + timedelta(hours=1)) is None  # paused: no API calls at all
    assert len(llm.calls) == calls
    script["technical"] = agreeing_script()["technical"]
    assert desk.debate("EUR_USD", NOW + timedelta(hours=7)) is not None  # retried after 6 hours


def test_spend_cap_and_halt_alerts():
    settings = replace(SETTINGS, claude=replace(SETTINGS.claude, daily_budget_usd=0.0))
    desk, broker, _ = make(settings=settings)
    desk.alerts = alerts = RecordingAlerts()
    desk.debate("EUR_USD", NOW)
    assert alerts.sent[-1][0] == "spend_cap" and "daily" in alerts.sent[-1][1]
    desk.executor.check_kill_switch(NOW)  # starts the budget
    broker.nav -= 200  # the whole 130 SGD budget is gone
    desk.cycle(NOW)
    assert alerts.sent[-1][0] == "halted" and "100 USD" in alerts.sent[-1][1]

