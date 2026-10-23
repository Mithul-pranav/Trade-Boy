import json
from datetime import date, datetime, time, timedelta, timezone

from tests.stock_fakes import NOW, bars, settings
from tradeboy.journal import Journal
from tradeboy.stocks import research
from tradeboy.stocks.market import NEW_YORK
from tradeboy.stocks.models import Bar
from tradeboy.stocks.team import QuantSeat, RiskQuant, calibration, execution_report, slippage_bps


def test_risk_quant_caps_a_second_stock_in_the_same_group():
    s = settings()
    rq = RiskQuant(1.0, s.group, max_correlated_risk_pct=3.0, max_group_risk_pct=1.5)
    daily = {"NVDA": bars(), "AMD": bars()}  # identical paths: correlation 1
    first = rq.assess("NVDA", 1000.0, [], daily)
    assert first["sides"]["long"]["max_share"] == 1.0
    after = rq.assess("AMD", 1000.0, [("NVDA", 1, 10.0)], daily)
    assert after["sides"]["long"]["max_share"] == 0.5 and "semiconductors" in after["sides"]["long"]["binding"]
    assert after["sides"]["short"]["max_share"] == 1.0  # a short offsets the group's long risk


def test_quant_seat_is_silent_without_a_promoted_rule_and_speaks_with_one():
    assert QuantSeat().view("NVDA", "earnings", {"excess_pct": 9.0})["significant"] is False
    seat = QuantSeat({"post_earnings_drift": {"threshold_pct": 5.0, "hold_days": 10, "avg_r": 0.12, "trades": 300, "t_stat": 2.6}})
    v = seat.view("NVDA", "earnings", {"excess_pct": -7.5})
    assert v["significant"] and v["stance"] == "short" and v["hold_days"] == 10
    assert seat.view("NVDA", "earnings", {"excess_pct": 2.0})["significant"] is False
    assert seat.view("NVDA", "news_burst", {"excess_pct": 9.0})["significant"] is False


def test_quant_trader_and_ml_researcher_reports():
    assert slippage_bps("buy", 100.0, 100.05) == 5.0
    assert slippage_bps("sell", 100.0, 100.05) == -5.0
    j = Journal(":memory:")
    j.set_state("execution:1:NVDA", json.dumps({"slippage_bps": 3.0, "stop_vs_atr": 1.5}))
    j.set_state("execution:2:AMD", json.dumps({"slippage_bps": 7.0, "stop_vs_atr": 2.5}))
    assert execution_report(j) == {"fills": 2, "avg_slippage_bps": 5.0, "worst_slippage_bps": 7.0, "median_stop_in_daily_ranges": 2.0}
    for i, (conf, result) in enumerate([(75, 50), (75, 30), (75, -20), (55, -10)]):
        j.record_call(f"d{i}", "news", "NVDA", "long", conf, 100, NOW, NOW)
        j.score_call(f"d{i}", "news", result)
    c = calibration(j)["news"]
    assert c["bands"]["70-80%"] == {"calls": 3, "right_pct": 67} and c["scored_calls"] == 4


# -- research -------------------------------------------------------------------------------------

def _days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bars(days, closes, wiggle=0.5):
    return [Bar(datetime.combine(d, time(16), NEW_YORK), c, c + wiggle, c - wiggle, c, 1e6) for d, c in zip(days, closes)]


def test_reaction_day_depends_on_when_the_report_was_filed():
    days = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]
    after_close = datetime(2026, 10, 21, 20, 5, tzinfo=timezone.utc)  # 16:05 New York
    before_open = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)  # 07:00 New York
    assert research.reaction_day(after_close, days) == 1
    assert research.reaction_day(before_open, days) == 1


def test_drift_is_found_when_it_exists_and_costs_are_paid():
    days = _days(date(2015, 1, 5), 400)
    closes, reports = [100.0] * 30, []
    price = 100.0
    for i in range(30, 400):
        if i % 60 == 0:  # a +8% earnings reaction, then 0.3% a day of drift for 10 days
            reports.append(datetime.combine(days[i - 1], time(17), NEW_YORK))
            price *= 1.08
        elif i % 60 <= 10:
            price *= 1.003
        closes.append(price)
    stock, bench = _bars(days, closes), _bars(days, [100.0] * 400)
    trades = research.simulate("NVDA", stock, bench, reports, {"cost_bps_per_side": 5, "borrow_bps_per_year": 30})
    h10 = [t for t in trades if t.variant == "t5_h10"]
    assert h10 and all(t.side == 1 and t.r > 0 for t in h10)
    free = research.simulate("NVDA", stock, bench, reports, {"cost_bps_per_side": 0})
    assert sum(t.r for t in free) > sum(t.r for t in trades)  # costs are charged


def test_gap_through_the_stop_fills_at_the_open():
    days = _days(date(2015, 1, 5), 40)
    closes = [100.0] * 25 + [94.0] * 3 + [100.0] * 12  # -6% reaction, then a gap up far through the short's stop
    stock = _bars(days, closes)
    stock[28] = Bar(stock[28].time, 100.0, 100.5, 99.5, 100.0, 1e6)
    trades = research.simulate("NVDA", stock, _bars(days, [100.0] * 40),
                               [datetime.combine(days[24], time(17), NEW_YORK)], {"cost_bps_per_side": 0})
    short = [t for t in trades if t.variant == "t5_h5"]
    assert short and short[0].side == -1 and short[0].how == "gapped through the stop"
    assert short[0].exit == 100.0 and short[0].r < -3  # the loss is the gap, far more than 1R


def test_evaluate_fails_noise_and_never_promotes_it(tmp_path):
    import random

    rng = random.Random(7)
    trades = [research.Trade("X", v, 1, d, d, d, 100, 100, 99, rng.gauss(0, 1), "held")
              for v in ("t3_h5", "t5_h5") for d in _days(date(2009, 1, 1), 4500)[::10]]
    plan = {"first_test": "2012-01-01", "holdout_start": "2025-10-01", "train_years": 3, "test_months": 6,
            "pass_bar": {"min_trades": 200, "min_avg_r": 0.05, "min_t_stat": 2.0, "min_profit_factor": 1.1,
                         "min_positive_windows_pct": 60.0, "holdout_min_avg_r": 0.0}}
    result = research.evaluate(trades, plan)
    assert result["passed"] is False
    promoted = tmp_path / "promoted.json"
    promoted.write_text("{}")
    assert research.promote(result, promoted) is False and not promoted.exists()
    assert "FAILED" in research.render(result)
