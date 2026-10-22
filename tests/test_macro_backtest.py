import math

import pytest

np = pytest.importorskip("numpy")  # the research extra: pip install -e ".[research]"
pd = pytest.importorskip("pandas")

from tradeboy import macro_backtest as mb
from tradeboy.macro_backtest import Costs, ExitRule

NO_COSTS = Costs(slippage_pips=0.0, financing_markup_pct=0.0)


def test_a_monthly_rate_is_only_used_two_months_later():
    s = pd.Series([1.0, 2.0], index=pd.to_datetime(["2020-01-01", "2020-02-01"], utc=True))
    known = mb._known_monthly(s)
    assert list(known.index.strftime("%Y-%m-%d")) == ["2020-03-01", "2020-04-01"]


def test_holm_never_makes_a_p_value_smaller():
    adj = mb.holm({"a": 0.01, "b": 0.02, "c": 0.5})
    assert adj["a"] == pytest.approx(0.03) and adj["b"] == pytest.approx(0.04) and adj["c"] == pytest.approx(0.5)


def test_parsers_read_central_bank_tables():
    rba = "Series ID,X,Y\n04-Feb-2026,+0.25,3.85\n23-Jan-1990,-0.50 to -1.00,17.00 to 17.50\n"
    assert mb._parse_rba(rba) == [("2026-02-04", 3.85), ("1990-01-23", 17.25)]
    boe = '<td align="left">18 Dec 25</td>\n<td align="right">  3.75</td>'
    assert mb._parse_boe(boe) == [("2025-12-18", 3.75)]
    boc = '"date","V39079"\n"2025-01-01","3.25"\n"2025-01-02","3.25"\n"2025-01-30","3.00"\n'
    assert mb._parse_boc(boc) == [("2025-01-01", 3.25), ("2025-01-30", 3.0)]


def _panel(signal, rets, carry=0.0, spread=0.0002, vol=0.10):
    n = len(rets)
    t = pd.date_range("2020-01-06 09:00", periods=n, freq="7D", tz="UTC")
    return pd.DataFrame({"time": t, "pair": "EUR_USD", "mid": 1.1, "spread": spread, "ret": rets,
                         "days": 7.0, "vol": vol, "carry": carry, "signal": signal})


def test_trading_costs_are_paid_only_when_the_position_changes():
    p = _panel([1, 1, 1], [0.0, 0.0, 0.0])
    r = mb.pair_returns(p, np.array([1.0, 1.0, 1.0]), NO_COSTS)
    assert r["cost"].iloc[0] == pytest.approx(0.0001)  # half the 2bp spread on a 1x position
    assert r["cost"].iloc[1:].sum() == 0


def test_a_long_earns_the_rate_gap_and_pays_the_markup():
    p = _panel([1], [0.0], carry=2.0)
    r = mb.pair_returns(p, np.array([1.0]), Costs(slippage_pips=0.0, financing_markup_pct=0.5))
    assert r["financing"].iloc[0] == pytest.approx((2.0 - 0.5) / 100 * 7 / 365)
    r = mb.pair_returns(p, np.array([-1.0]), Costs(slippage_pips=0.0, financing_markup_pct=0.5))
    assert r["financing"].iloc[0] == pytest.approx((-2.0 - 0.5) / 100 * 7 / 365)


def _h4(prices, highs=None, lows=None, start="2020-01-06 09:00"):
    """4h candles with no spread: open = previous close."""
    n = len(prices)
    t = pd.date_range(start, periods=n, freq="4h", tz="UTC")
    c = np.array(prices, float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.array(highs, float) if highs is not None else np.maximum(o, c)
    lo = np.array(lows, float) if lows is not None else np.minimum(o, c)
    return {"time": pd.DatetimeIndex(t), "bo": o, "bh": h, "bl": lo, "bc": c, "ao": o, "ah": h, "al": lo, "ac": c}


def _weeks(h4, signal):
    # One weekly row every 42 candles (7 days of 4h candles).
    times = h4["time"][::42][:len(signal)]
    return pd.DataFrame({"time": times, "vol": 0.10 * math.sqrt(52) / 1.0, "carry": 0.0, "signal": signal,
                         "pair": "EUR_USD"})


def test_stop_is_hit_before_the_target_in_the_same_candle():
    prices = [1.0] * 200
    lows, highs = [1.0] * 200, [1.0] * 200
    lows[3], highs[3] = 0.5, 1.5  # one wild candle touches both
    h4 = _h4(prices, highs, lows)
    g = _weeks(h4, [1, 1, 1, 1])  # weekly sigma = 0.10 of price, so the stop is at 0.8 and the target at 1.4
    t = mb.run_exit("EUR_USD", h4, g, 0, 1, ExitRule("x", "", stop_sigmas=2.0, target_r=2.0), None, NO_COSTS)
    assert t["how"] == "stop" and t["net"] < 0


def test_signal_flip_closes_at_the_next_weekly_check():
    h4 = _h4([1.0] * 42 + [1.1] * 42 + [1.2] * 42 + [1.3] * 42)
    g = _weeks(h4, [1, 1, -1, -1])
    t = mb.run_exit("EUR_USD", h4, g, 0, 1, ExitRule("flip", "", on_flip=True), None, NO_COSTS)
    assert t["how"] == "signal" and t["exit_time"] == g["time"].iloc[2]


def test_trailing_stop_locks_in_a_gain():
    prices = [1.0 + 0.01 * i for i in range(60)] + [1.3] * 10 + [1.0] * 60
    h4 = _h4(prices)
    g = _weeks(h4, [1, 1, 1, 1])
    t = mb.run_exit("EUR_USD", h4, g, 0, 1, ExitRule("trail", "", stop_sigmas=2.0, trailing=True, max_weeks=26),
                    None, NO_COSTS)
    assert t["how"] == "stop" and t["net"] > 0


def test_a_rate_move_against_the_trade_closes_it():
    h4 = _h4([1.0] * 168)
    g = _weeks(h4, [1, 1, 1, 1])
    decisions = pd.DataFrame({"date": [pd.Timestamp("2020-01-08", tz="UTC")], "currency": ["EUR"],
                              "direction": [-1], "change": [-0.25]})
    t = mb.run_exit("EUR_USD", h4, g, 0, 1, ExitRule("p", "", on_flip=True, on_policy=True), decisions, NO_COSTS)
    assert t["how"] == "central bank" and t["exit_time"] >= pd.Timestamp("2020-01-09", tz="UTC")


def test_debate_economics_break_even_capital():
    rows = mb.debate_economics(0.05, mb.Plan())  # 5% a year at 10% volatility
    weekly = rows[1]
    assert weekly["api_cost_usd"] == pytest.approx(7.8)
    assert weekly["expected_profit_usd"] == pytest.approx(5.0)
    assert weekly["capital_to_break_even_usd"] == 156
