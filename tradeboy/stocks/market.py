"""Plain-number summaries of stock bars, in percent, for the agents and the event scanner."""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

from ..market import atr, ema, rsi
from .models import Bar

NEW_YORK = ZoneInfo("America/New_York")


def ny_date(t: datetime):
    return t.astimezone(NEW_YORK).date()


def daily_returns(bars: list[Bar]) -> list[float]:
    return [b.close / a.close - 1 for a, b in zip(bars, bars[1:]) if a.close > 0]


def excess_returns(stock: list[Bar], bench: list[Bar]) -> list[float]:
    """Daily return of the stock minus the benchmark's, on the days both traded."""
    b = {ny_date(x.time): x.close for x in bench}
    rows = [(ny_date(x.time), x.close) for x in stock if ny_date(x.time) in b]
    return [(c2 / c1) - (b[d2] / b[d1]) for (d1, c1), (d2, c2) in zip(rows, rows[1:]) if c1 > 0 and b[d1] > 0]


def atr_pct(bars: list[Bar], period: int = 14) -> float:
    return atr(bars, period) / bars[-1].close * 100 if bars and bars[-1].close else 0.0


def summarize(bars: list[Bar], bench: list[Bar] | None = None) -> dict:
    closes = [b.close for b in bars]
    last = closes[-1]
    fast, slow = ema(closes, 20), ema(closes, 50)
    recent = bars[-20:]
    high, low = max(b.high for b in recent), min(b.low for b in recent)
    trend = "up" if fast > slow and last > fast else "down" if fast < slow and last < fast else "mixed"
    out = {
        "last_close": round(last, 2),
        "trend": trend,
        "ema20": round(fast, 2),
        "ema50": round(slow, 2),
        "rsi14": round(rsi(closes), 1),
        "atr14_pct": round(atr_pct(bars), 2),
        "range20_high": round(high, 2),
        "range20_low": round(low, 2),
        "pct_below_range_high": round((high - last) / last * 100, 2),
        "pct_above_range_low": round((last - low) / last * 100, 2),
        "change_over_window_pct": round((last / closes[0] - 1) * 100, 2),
        "avg_volume_20": round(sum(b.volume for b in recent) / len(recent)),
        "last_volume": round(bars[-1].volume),
        "last_bar_time": bars[-1].time.isoformat(),
    }
    if bench:
        ex = excess_returns(bars, bench)
        if len(ex) >= 20:
            out["vs_benchmark_20_bars_pct"] = round(sum(ex[-20:]) * 100, 2)
    return out


def recent_bars(bars: list[Bar], count: int = 12, fmt: str = "%m-%d %H:%M") -> list[list]:
    return [[b.time.astimezone(NEW_YORK).strftime(fmt), round(b.open, 2), round(b.high, 2), round(b.low, 2),
             round(b.close, 2), round(b.volume)] for b in bars[-count:]]


def move_today(stock: list[Bar], bench: list[Bar], price: float, bench_price: float, today) -> dict | None:
    """Today's move against the previous close, and against the benchmark, with how unusual it is."""
    prev = [b for b in stock if ny_date(b.time) < today]
    prev_bench = [b for b in bench if ny_date(b.time) < today]
    if len(prev) < 25 or not prev_bench:
        return None
    move = price / prev[-1].close - 1
    bench_move = bench_price / prev_bench[-1].close - 1
    ex = excess_returns(prev, prev_bench)[-60:]
    sd = statistics.pstdev(ex) if len(ex) >= 20 else None
    return {
        "move_pct": round(move * 100, 2),
        "benchmark_move_pct": round(bench_move * 100, 2),
        "excess_pct": round((move - bench_move) * 100, 2),
        "excess_sigma": round((move - bench_move) / sd, 1) if sd else None,
        "prev_close": prev[-1].close,
    }


def log_returns(bars: list[Bar]) -> list[float]:
    return [math.log(b.close / a.close) for a, b in zip(bars, bars[1:]) if a.close > 0 and b.close > 0]
