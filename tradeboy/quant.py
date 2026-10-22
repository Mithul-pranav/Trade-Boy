"""The quant seat: trade signals that are pure statistics, computed in code, never Claude's opinion.

Every signal is a fixed rule on prices (no fitted numbers), so the backtester can test it on years of
history. The seat only counts a signal as evidence once the backtest has shown an edge for it on data
it was not chosen on (see backtest.py); the debate then sees how strong that edge was, in numbers.

The same `snapshot` function feeds both the live desk and the backtest, so what was tested is what runs.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import ROOT
from .market import atr, find_setups, summarize
from .models import pip_size

VALIDATION_PATH = ROOT / "data" / "backtest" / "quant_validation.json"
# Bars of each timeframe a snapshot reads. H1 and H4 match the desk's code screen (100 each).
LOOKBACK = {"H1": 100, "H4": 100, "D": 130}


def snapshot(instrument: str, h1: list, h4: list, daily: list, carry_pct: float | None = None) -> dict:
    """Everything the signals look at, from the complete candles up to now (oldest first).

    carry_pct is the base currency's short-term interest rate minus the quote currency's, if known.
    """
    h1, h4, daily = h1[-LOOKBACK["H1"]:], h4[-LOOKBACK["H4"]:], daily[-LOOKBACK["D"]:]
    pip = pip_size(instrument)
    s1, s4 = summarize(instrument, h1), summarize(instrument, h4)
    closes = [c.close for c in h1[-24:]]
    mean = sum(closes) / len(closes)
    std = math.sqrt(sum((c - mean) ** 2 for c in closes) / len(closes)) or pip
    d_closes = [c.close for c in daily]
    last_d = d_closes[-1]

    def change(days: int) -> float | None:
        return round((last_d - d_closes[-1 - days]) / pip, 1) if len(d_closes) > days else None

    prior = daily[-21:-1]
    return {
        "h1": s1,
        "h4": s4,
        "setups": find_setups(s1, s4),
        "h1_atr_pips": s1["atr14_pips"],
        "d_atr_pips": round(atr(daily) / pip, 1),
        "z24": round((h1[-1].close - mean) / std, 2),
        "d_change_20": change(20),
        "d_change_60": change(60),
        "d_change_120": change(120),
        "d_breakout_20": (
            "up" if prior and last_d > max(c.high for c in prior)
            else "down" if prior and last_d < min(c.low for c in prior) else None
        ),
        "carry_pct": carry_pct,
    }


# -- the signals: each returns "long", "short" or None (nothing to say) -------------------------------


def _setup_side(setups: list[str], h1: dict) -> str | None:
    """The direction each code-screen setup naturally implies; None if the setups point both ways."""
    sides = set()
    for reason in setups:
        if "uptrend" in reason or "bottom of a 4h range" in reason:
            sides.add("long")
        elif "downtrend" in reason or "top of a 4h range" in reason:
            sides.add("short")
        elif "breakout" in reason:
            sides.add("long" if h1["pips_from_range_high"] <= h1["pips_from_range_low"] else "short")
    return sides.pop() if len(sides) == 1 else None


def _only(setups: list[str], word: str) -> list[str]:
    return [r for r in setups if word in r]


def _sign(value: float | None, threshold: float = 0.0) -> str | None:
    if value is None or abs(value) <= threshold:
        return None
    return "long" if value > 0 else "short"


@dataclass(frozen=True)
class Signal:
    name: str
    description: str
    rule: Callable[[dict], str | None]


SIGNALS = [
    Signal("screen_all", "The desk's code screen, trading the direction each setup implies",
           lambda s: _setup_side(s["setups"], s["h1"])),
    Signal("screen_pullback", "Code screen: 4h trend with a 1h pullback, traded with the trend",
           lambda s: _setup_side(_only(s["setups"], "trend with"), s["h1"])),
    Signal("screen_range_fade", "Code screen: price at the edge of a 4h range, traded back into the range",
           lambda s: _setup_side(_only(s["setups"], "range"), s["h1"])),
    Signal("screen_breakout", "Code screen: 1h breakout to a 20-bar extreme, traded with the breakout",
           lambda s: _setup_side(_only(s["setups"], "breakout"), s["h1"])),
    Signal("momentum_20d", "Time-series momentum: the direction of the last 20 trading days",
           lambda s: _sign(s["d_change_20"])),
    Signal("momentum_60d", "Time-series momentum: the direction of the last 60 trading days",
           lambda s: _sign(s["d_change_60"])),
    Signal("momentum_120d", "Time-series momentum: the direction of the last 120 trading days",
           lambda s: _sign(s["d_change_120"])),
    Signal("daily_breakout", "Daily close beyond the prior 20-day high or low, traded with the break",
           lambda s: {"up": "long", "down": "short"}.get(s["d_breakout_20"])),
    Signal("h1_stretch_fade", "1h close more than 2 standard deviations from its 24-hour average, faded",
           lambda s: _sign(-s["z24"], 2.0)),
    Signal("carry", "Carry: hold the currency with the higher interest rate (gap above 0.5 points)",
           lambda s: _sign(s["carry_pct"], 0.5)),
    Signal("h1_rsi_fade", "1h RSI below 25 (buy) or above 75 (sell)",
           lambda s: "long" if s["h1"]["rsi14"] < 25 else "short" if s["h1"]["rsi14"] > 75 else None),
]
SIGNALS_BY_NAME = {s.name: s for s in SIGNALS}


def flip(side: str | None) -> str | None:
    return {"long": "short", "short": "long"}.get(side)


# -- the seat in a debate --------------------------------------------------------------------------------


def load_validation(path: Path = VALIDATION_PATH) -> dict | None:
    """The backtest's latest verdict on each strategy, or None if no backtest has been run."""
    return json.loads(path.read_text()) if path.exists() else None


class QuantSeat:
    """Gives the debate a view built only from strategies that showed an out-of-sample edge."""

    def __init__(self, validation: dict | None):
        self.validation = validation

    def view(self, instrument: str, h1: list, h4: list, daily: list, carry_pct: float | None = None) -> dict:
        snap = snapshot(instrument, h1, h4, daily, carry_pct)
        validated = (self.validation or {}).get("validated", [])
        firing = []
        for strat in validated:
            side = SIGNALS_BY_NAME[strat["signal"]].rule(snap)
            if strat.get("inverted"):
                side = flip(side)
            if side:
                firing.append({**strat, "side": side})
        readings = {s.name: s.rule(snap) for s in SIGNALS}
        facts = [
            f"20/60/120-day change: {snap['d_change_20']} / {snap['d_change_60']} / {snap['d_change_120']} pips",
            f"1h close is {snap['z24']} standard deviations from its 24-hour average; 1h RSI {snap['h1']['rsi14']}",
            f"ATR: 1h {snap['h1_atr_pips']} pips, daily {snap['d_atr_pips']} pips",
            f"Interest rate gap (base minus quote, 3-month rates): "
            f"{'unknown' if carry_pct is None else f'{carry_pct:+.2f} points'}",
            f"Code screen setups: {', '.join(snap['setups']) or 'none'}",
        ]
        if not firing:
            # No tested edge applies: the seat still has to name a side, so it follows 60-day momentum,
            # and says plainly that this is not a statistical edge.
            side = readings["momentum_60d"] or "long"
            why = ("No backtest has been run yet" if self.validation is None
                   else "No strategy with a proven out-of-sample edge is signalling on this pair now")
            return {
                "stance": side, "confidence": 50, "significant": False,
                "thesis": f"{why}. Default lean: 60-day momentum is {side}. This is NOT a statistical edge; "
                          "treat it as a coin flip.",
                "evidence": facts, "invalidation": "Not applicable: there is no tested edge here.",
                "hold_hours": 24, "signals_now": readings, "validated_strategies_firing": [],
            }
        score = {"long": 0.0, "short": 0.0}
        for f in firing:
            score[f["side"]] += f["t_stat"]
        side = max(score, key=score.get)
        backing = [f for f in firing if f["side"] == side]
        best = max(backing, key=lambda f: f["t_stat"])
        # Confidence is the backtested win rate of the strongest strategy behind this side.
        confidence = int(round(min(max(best["win_rate_pct"], 50), 75)))
        return {
            "stance": side, "confidence": confidence, "significant": True,
            "thesis": (
                f"{best['label']} says {side}. Out of sample it won {best['win_rate_pct']}% of "
                f"{best['trades']} trades, averaging {best['avg_r']:+.2f}R after spread and costs "
                f"(t-stat {best['t_stat']})."
            ),
            "evidence": facts + [
                f"{f['label']}: {f['side']}, {f['trades']} out-of-sample trades, {f['avg_r']:+.2f}R average, "
                f"t-stat {f['t_stat']}" for f in firing
            ],
            "invalidation": f"A stop at {best['stop_atr']}x the 1h ATR; the statistics are about many trades, "
                            "so any single one can lose.",
            "hold_hours": best["max_hours"], "signals_now": readings, "validated_strategies_firing": firing,
        }
