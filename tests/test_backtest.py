from datetime import datetime, timedelta, timezone

import pytest

from tradeboy import backtest as bt
from tradeboy import history
from tradeboy.backtest import Costs, Exit, Selection, Trade

T0 = datetime(2020, 1, 6, tzinfo=timezone.utc)
COSTS = Costs(slippage_pips=0.0, financing_markup_pct=0.0)
EXIT = Exit("test", 1.0, "h1", 2.0, 5)


def bar(i, mid, high=None, low=None, open_=None, spread=0.0002):
    """An hourly bar around a mid price; bid and ask sit half the spread either side."""
    o = mid if open_ is None else open_
    h, lo = high if high is not None else max(o, mid), low if low is not None else min(o, mid)
    half = spread / 2
    return history.Bar(T0 + timedelta(hours=i), o, h, lo, mid, 100,
                       o - half, h - half, lo - half, mid - half, o + half, h + half, lo + half, mid + half)


def trade(bars, side="long", stop_pips=10.0, carry=None, costs=COSTS):
    t, _ = bt._run_trade("EUR_USD", bars, 0, side, stop_pips, EXIT, costs, 0.0001, "k", 2.0, carry)
    return t


def test_long_pays_the_spread_and_reaches_its_target():
    # Entry at the ask (1.1001); a 2R target at 1.1021 must be reached by the bid.
    bars = [bar(0, 1.1000), bar(1, 1.1010, high=1.1030)]
    t = trade(bars)
    assert t.how == "target" and t.entry == pytest.approx(1.1001)
    assert t.r == pytest.approx(2.0)


def test_stop_counts_first_when_stop_and_target_share_a_bar():
    bars = [bar(0, 1.1000), bar(1, 1.1000, high=1.1050, low=1.0950)]
    assert trade(bars).how == "stop"


def test_gap_through_the_stop_fills_at_the_worse_open():
    bars = [bar(0, 1.1000), bar(1, 1.0960, open_=1.0960)]
    t = trade(bars)
    assert t.how == "stop" and t.exit == pytest.approx(1.0959)
    assert t.r < -1


def test_short_is_closed_at_the_ask_after_the_time_limit():
    bars = [bar(i, 1.1000) for i in range(8)]
    t = trade(bars, side="short")
    assert t.how == "time" and t.exit_time == T0 + timedelta(hours=5)
    # Sold at the bid, bought back at the ask: the whole spread is lost.
    assert t.pips == pytest.approx(-2.0)


def test_slippage_and_financing_are_charged():
    bars = [bar(i, 1.1000) for i in range(30)]
    long_exit = Exit("day", 1.0, "h1", None, 24)
    base = bt._run_trade("EUR_USD", bars, 0, "long", 10.0, long_exit, COSTS, 0.0001, "k", 2.0, None)[0]
    costly = Costs(slippage_pips=0.5, financing_markup_pct=0.0)
    slipped = bt._run_trade("EUR_USD", bars, 0, "long", 10.0, long_exit, costly, 0.0001, "k", 2.0, None)[0]
    assert slipped.pips == pytest.approx(base.pips - 1.0)
    # Long the currency with a 3.65-point higher rate for one day earns 0.01% of the price, about 1.1 pips.
    earning = bt._run_trade("EUR_USD", bars, 0, "long", 10.0, long_exit, COSTS, 0.0001, "k", 2.0, 3.65)[0]
    assert earning.pips - base.pips == pytest.approx(1.1, abs=0.05)


def _t(day, r, pair="EUR_USD", strategy="s", hours=2):
    entry = T0 + timedelta(days=day)
    return Trade(pair, strategy, "long", entry, entry + timedelta(hours=hours), 1.0, 1.0, 10.0, 1.0, r * 10, r, "time")


def test_stats_in_plain_numbers():
    s = bt.stats([_t(0, 2.0), _t(1, -1.0), _t(2, -1.0), _t(40, 1.0)])
    assert s["trades"] == 4 and s["win_rate_pct"] == 50.0
    assert s["avg_r"] == 0.25 and s["profit_factor"] == 1.5 and s["max_drawdown_r"] == 2.0


def test_walk_forward_trades_only_what_worked_before_and_keeps_the_holdout_out():
    good = [_t(d, 0.5 if d % 3 else -0.5, strategy="momentum_20d/swing") for d in range(0, 2000, 2)]
    bad = [_t(d, -0.3, strategy="carry/swing") for d in range(1, 2000, 2)]
    first_test = T0 + timedelta(days=3 * 366)
    holdout = T0 + timedelta(days=1500)
    windows, oos = bt.walk_forward(good + bad, first_test, holdout, rule=Selection(min_trades=30))
    assert windows and all(w.chosen and w.chosen[0]["key"] == "momentum_20d/swing" for w in windows)
    assert all(len(w.chosen) == 1 for w in windows)
    assert oos and all(first_test <= t.entry_time and t.exit_time <= holdout for t in oos)


def test_desk_simulation_applies_the_desks_limits_and_api_cost():
    trades = [_t(0, 1.0, pair) for pair in ("EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD")] + [_t(0, 1.0)]
    d = bt.desk_simulation(trades, budget=100, risk_pct=1.0, api_cost_per_trade=0.15)
    # 3 open at most and one per pair, so 3 of the 5 trades are taken.
    assert d["trades"] == 3 and d["end_equity_usd"] == pytest.approx(103.03)
    assert d["api_cost_usd"] == 0.45 and d["net_after_api_usd"] == pytest.approx(2.58)


def test_pass_bar_needs_the_debate_paid_for():
    oos = {"trades": 500, "avg_r": 0.1, "t_stat": 3.0, "profit_factor": 1.3}
    window = bt.Window(T0, T0, T0, test={"trades": 10, "avg_r": 0.1})
    checks = bt.check_pass_bar(bt.PassBar(), oos, {"avg_r": 0.05}, [window], {"max_drawdown_pct": 10.0, "trades": 9},
                               {"trades": 50, "avg_r": 0.1}, risk_usd=0.5, api_cost=0.15)
    failed = [c["check"] for c in checks if not c["passed"]]
    assert failed == ["Pays for its own debate"]


def test_history_round_trip_and_rates_known_only_after_publication(tmp_path):
    class Client:
        def _request(self, method, path, params):
            start = datetime.fromisoformat(params["from"])
            if start >= T0 + timedelta(hours=3):
                return {"candles": []}
            px = {k: "1.1" for k in "ohlc"}
            return {"candles": [{"time": (start + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
                                 "complete": True, "volume": 5, "mid": px, "bid": px, "ask": px} for i in range(3)]}

    history.download(Client(), "EUR_USD", "H1", T0, T0 + timedelta(days=1), folder=tmp_path)
    bars = history.load("EUR_USD", "H1", folder=tmp_path)
    assert [b.time for b in bars] == [T0 + timedelta(hours=i) for i in range(3)]
    assert bars[0].ask_close == 1.1

    (tmp_path / "short_rates.csv").write_text("currency,month,rate_pct\nEUR,2020-01-01,1.0\nUSD,2020-01-01,3.0\n")
    rates = history.ShortRates.load(tmp_path)
    assert rates.differential("EUR_USD", datetime(2020, 2, 15, tzinfo=timezone.utc)) is None  # not yet published
    assert rates.differential("EUR_USD", datetime(2020, 3, 1, tzinfo=timezone.utc)) == -2.0
