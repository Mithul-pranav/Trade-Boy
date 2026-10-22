"""Plain-number market summaries built from OANDA candles, for the scanner and the technical analyst."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Candle, pip_size

NEW_YORK = ZoneInfo("America/New_York")


def market_open(now: datetime) -> bool:
    """Forex trades from Sunday 17:00 to Friday 17:00 New York time."""
    ny = now.astimezone(NEW_YORK)
    if ny.weekday() == 5:  # Saturday
        return False
    if ny.weekday() == 6:  # Sunday
        return ny.hour >= 17
    if ny.weekday() == 4:  # Friday
        return ny.hour < 17
    return True


def ema(values: list[float], period: int) -> float:
    k = 2 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = v * k + result * (1 - k)
    return result


def atr(candles: list[Candle], period: int = 14) -> float:
    ranges = []
    for prev, c in zip(candles, candles[1:]):
        ranges.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    window = ranges[-period:]
    return sum(window) / len(window) if window else 0.0


def rsi(closes: list[float], period: int = 14) -> float:
    gains, losses = 0.0, 0.0
    for prev, cur in zip(closes[-period - 1:], closes[-period:]):
        change = cur - prev
        gains += max(change, 0)
        losses += max(-change, 0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def summarize(instrument: str, candles: list[Candle]) -> dict:
    """Trend, momentum, volatility and range for one timeframe, in pips where that helps."""
    pip = pip_size(instrument)
    closes = [c.close for c in candles]
    last = closes[-1]
    fast, slow = ema(closes, 20), ema(closes, 50)
    recent = candles[-20:]
    high, low = max(c.high for c in recent), min(c.low for c in recent)
    trend = "up" if fast > slow and last > fast else "down" if fast < slow and last < fast else "mixed"
    return {
        "last_close": last,
        "trend": trend,
        "ema20": round(fast, 5),
        "ema50": round(slow, 5),
        "rsi14": round(rsi(closes), 1),
        "atr14_pips": round(atr(candles) / pip, 1),
        "range20_high": high,
        "range20_low": low,
        "pips_from_range_high": round((high - last) / pip, 1),
        "pips_from_range_low": round((last - low) / pip, 1),
        "change_over_window_pips": round((last - closes[0]) / pip, 1),
        "last_candle_time": candles[-1].time.isoformat(),
    }


def recent_bars(candles: list[Candle], count: int = 12) -> list[list]:
    """The last few candles as compact [time, open, high, low, close] rows."""
    return [[c.time.strftime("%m-%d %H:%M"), c.open, c.high, c.low, c.close] for c in candles[-count:]]


def find_setups(h1: dict, h4: dict) -> list[str]:
    """Free, code-only screen: the reasons this pair might be worth a (paid) debate, or none.

    h1 and h4 are `summarize` outputs. The rules are deliberately simple; they only decide whether
    the agents look at a pair, never whether to trade it.
    """
    reasons = []
    h4_atr = h4["atr14_pips"] or 1.0
    h4_range = h4["pips_from_range_high"] + h4["pips_from_range_low"]
    if h4["trend"] == "up" and h1["rsi14"] < 45:
        reasons.append(f"4h uptrend with a 1h pullback (RSI {h1['rsi14']})")
    if h4["trend"] == "down" and h1["rsi14"] > 55:
        reasons.append(f"4h downtrend with a 1h bounce (RSI {h1['rsi14']})")
    if h4["trend"] == "mixed" and h4_range >= 3 * h4_atr:
        if h4["pips_from_range_high"] <= 0.5 * h4_atr:
            reasons.append("testing the top of a 4h range")
        elif h4["pips_from_range_low"] <= 0.5 * h4_atr:
            reasons.append("testing the bottom of a 4h range")
    if abs(h1["change_over_window_pips"]) >= 4 * (h1["atr14_pips"] or 1.0) and (
        h1["pips_from_range_high"] <= 0.2 * h1["atr14_pips"] or h1["pips_from_range_low"] <= 0.2 * h1["atr14_pips"]
    ):
        reasons.append("1h breakout to a new 20-bar extreme")
    return reasons
