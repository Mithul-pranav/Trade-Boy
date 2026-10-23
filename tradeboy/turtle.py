"""The classic Turtle trading rules (Dennis and Eckhardt, 1983) on daily forex candles, in code.

The rules are the published ones, not fitted to this data:
- N is the 20-day average true range (Wilder's smoothing).
- System 1 enters on a break of the 20-day high or low and exits on a break of the 10-day low or high.
  It skips a breakout when the previous one would have been a winner, but always takes a 55-day breakout.
- System 2 enters on a break of the 55-day high or low and exits on a break of the 20-day low or high.
- Every unit has a stop 2N from its entry. A unit is added every 0.5N in the trade's favour, up to 4,
  and all stops then move to 2N from the newest entry.

Fills are honest: a buy pays the ask and a sell gets the bid (half the previous day's closing spread on
each side of mid, plus slippage), a gap through a level fills at the open, and when a stop and an exit
could both have hit on one day the worse price is used. Overnight financing is charged every day held.

R is profit in units of one unit's risk at entry (2N): at the desk's 0.5% per unit, +1R is +0.5%.
"""

from __future__ import annotations

import bisect
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import timedelta

from .backtest import Trade
from .models import pip_size

DAY = timedelta(days=1)


@dataclass(frozen=True)
class Variant:
    name: str
    entry_days: int
    exit_days: int
    skip_after_winner: bool  # System 1's filter
    failsafe_days: int | None  # a breakout this long is taken even when the filter says skip
    max_units: int  # 1 means no pyramiding


VARIANTS = [
    Variant("turtle_s1", 20, 10, True, 55, 4),
    Variant("turtle_s2", 55, 20, False, None, 4),
    Variant("turtle_s1_single", 20, 10, True, 55, 1),
    Variant("turtle_s2_single", 55, 20, False, None, 1),
]
VARIANTS_BY_NAME = {v.name: v for v in VARIANTS}
N_DAYS = 20
STOP_N = 2.0
ADD_EVERY_N = 0.5


def spread_of(bar) -> float:
    """A candle's closing spread; 0 for live candles that only carry mid prices."""
    ask, bid = getattr(bar, "ask_close", None), getattr(bar, "bid_close", None)
    return max(ask - bid, 0.0) if ask is not None and bid is not None else 0.0


def daily_spreads(bars, intraday=None) -> list[float]:
    """Each day's typical spread: the median closing spread of its 4-hour candles."""
    return [statistics.median(spread_of(s) for s in subs) for subs in _sub_bars(bars, intraday)]


def true_ranges(bars) -> list[float]:
    out = [bars[0].high - bars[0].low]
    for prev, b in zip(bars, bars[1:]):
        out.append(max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close)))
    return out


def wilder_n(bars, days: int = N_DAYS) -> list[float | None]:
    """N for each day, known at that day's close (None until there are enough days)."""
    tr = true_ranges(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) <= days:
        return out
    n = sum(tr[1:days + 1]) / days
    out[days] = n
    for i in range(days + 1, len(bars)):
        n = (n * (days - 1) + tr[i]) / days
        out[i] = n
    return out


def _channel(bars, i: int, days: int) -> tuple[float, float] | None:
    """Highest high and lowest low of the `days` days before day i."""
    if i < days:
        return None
    window = bars[i - days:i]
    return max(b.high for b in window), min(b.low for b in window)


@dataclass
class _Position:
    side: int  # +1 long, -1 short
    fills: list[float]  # what each unit paid, after spread and slippage
    last_mid: float  # mid price of the newest unit, from which the next add is measured
    n0: float  # N when the first unit was bought: one unit's risk is STOP_N * n0
    stop: float  # mid price
    start: int  # day index of the first unit
    spread_pips: float


def _sub_bars(bars, intraday) -> list[list]:
    """The 4-hour candles inside each daily candle, in order (the daily candle itself without them)."""
    if intraday is None:
        return [[b] for b in bars]
    starts = [b.time for b in bars]
    out: list[list] = [[] for _ in bars]
    for h in intraday:
        k = bisect.bisect_right(starts, h.time) - 1
        if k >= 0 and h.time < starts[k] + DAY + timedelta(hours=2):
            out[k].append(h)
    return [subs or [b] for subs, b in zip(out, bars)]


def _breakout(bars, i: int, days: int) -> int:
    """+1 if day i trades above the prior `days`-day high, -1 below the low, 0 otherwise (both: 0)."""
    ch = _channel(bars, i, days)
    if ch is None:
        return 0
    up, down = bars[i].high > ch[0], bars[i].low < ch[1]
    return 0 if up == down else (1 if up else -1)


def _hypothetical_winners(bars, variant: Variant, ns) -> list[tuple[int, int, bool]]:
    """System 1's filter: every entry breakout traded alone with one unit at mid, (start day, end day, won)."""
    out = []
    i = variant.entry_days
    while i < len(bars):
        side = _breakout(bars, i, variant.entry_days)
        if side == 0 or ns[i - 1] is None:
            i += 1
            continue
        hi, lo = _channel(bars, i, variant.entry_days)
        entry = max(hi, bars[i].open) if side > 0 else min(lo, bars[i].open)
        stop = entry - side * STOP_N * ns[i - 1]
        end, won = len(bars) - 1, False
        for j in range(i + 1, len(bars)):
            b = bars[j]
            ex_hi, ex_lo = _channel(bars, j, variant.exit_days)
            hits = []
            if (b.low <= stop) if side > 0 else (b.high >= stop):
                hits.append(stop)
            if (b.low < ex_lo) if side > 0 else (b.high > ex_hi):
                hits.append(ex_lo if side > 0 else ex_hi)
            if hits:
                exit_ = min(hits) if side > 0 else max(hits)
                end, won = j, (exit_ - entry) * side > 0
                break
        out.append((i, end, won))
        i = end + 1
    return out


def simulate(pair: str, bars, variant: Variant, rates=None, slippage_pips: float = 0.2,
             markup_pct: float = 0.8, intraday=None) -> tuple[list[Trade], _Position | None]:
    """All the variant's trades on one pair, and the position still open at the end (if any).

    The channels and N come from daily candles; `intraday` (4-hour candles with bid and ask) gives the
    order of events inside each day, so a day's low isn't wrongly taken to come after its breakout.
    Within one 4-hour candle a stop only counts after an entry or add if the candle closes beyond it.
    """
    pip = pip_size(pair)
    slip = slippage_pips * pip
    ns = wilder_n(bars)
    days_subs = _sub_bars(bars, intraday)
    hyp = _hypothetical_winners(bars, variant, ns) if variant.skip_after_winner else []
    trades: list[Trade] = []
    pos: _Position | None = None
    # The spread paid is the median closing spread of the last 6 candles: a typical day's spread, without
    # betting that every fill lands at the wide 17:00 New York rollover.
    recent = deque([spread_of(bars[0])], maxlen=6)
    first = max(variant.entry_days, variant.failsafe_days or 0, N_DAYS + 1)
    step = DAY if intraday is None else timedelta(hours=4)  # length of one candle in days_subs

    def buy(mid, spread):
        return mid + spread / 2 + slip

    def sell(mid, spread):
        return mid - spread / 2 - slip

    for i in range(first, len(bars)):
        n = ns[i - 1]
        entry_ch = _channel(bars, i, variant.entry_days)
        fail_ch = _channel(bars, i, variant.failsafe_days) if variant.failsafe_days else None
        exit_ch = _channel(bars, i, variant.exit_days)
        skip = False
        if variant.skip_after_winner:
            last = [h for h in hyp if h[0] < i]
            skip = bool(last and last[-1][1] < i and last[-1][2])
        traded_today = False
        for s in days_subs[i]:
            spread = statistics.median(recent)
            recent.append(spread_of(s))
            fresh = False  # a unit was bought in this candle
            if pos is None:
                if traded_today or n is None or n <= 0:
                    continue
                side, level = 0, None
                for ch, allowed in ((entry_ch, not skip), (fail_ch, True)):
                    if ch is None or not allowed:
                        continue
                    if s.high > ch[0] and s.low >= ch[1]:
                        side, level = 1, ch[0]
                    elif s.low < ch[1] and s.high <= ch[0]:
                        side, level = -1, ch[1]
                    if side:
                        break
                if not side:
                    continue
                mid = max(level, s.open) if side > 0 else min(level, s.open)
                fill = buy(mid, spread) if side > 0 else sell(mid, spread)
                pos = _Position(side, [fill], mid, n, mid - side * STOP_N * n, i, spread / pip)
                traded_today = fresh = True
            # Add units every 0.5N in the trade's favour.
            while len(pos.fills) < variant.max_units:
                level = pos.last_mid + pos.side * ADD_EVERY_N * pos.n0
                if not ((s.high >= level) if pos.side > 0 else (s.low <= level)):
                    break
                mid = max(level, s.open) if pos.side > 0 else min(level, s.open)
                pos.fills.append(buy(mid, spread) if pos.side > 0 else sell(mid, spread))
                pos.last_mid = mid
                pos.stop = mid - pos.side * STOP_N * pos.n0
                fresh = True
            hits = []
            touched = (s.close if fresh else (s.low if pos.side > 0 else s.high))
            if (touched <= pos.stop) if pos.side > 0 else (touched >= pos.stop):
                hits.append((min(pos.stop, s.open) if pos.side > 0 else max(pos.stop, s.open), "stop"))
            if exit_ch is not None and i > pos.start:
                level = exit_ch[1] if pos.side > 0 else exit_ch[0]
                if (s.low < level) if pos.side > 0 else (s.high > level):
                    hits.append((min(level, s.open) if pos.side > 0 else max(level, s.open), "channel"))
            if hits:
                mid, how = min(hits) if pos.side > 0 else max(hits)
                out = sell(mid, spread) if pos.side > 0 else buy(mid, spread)
                trades.append(_close(pair, bars, pos, out, how, rates, markup_pct, s.time + step, variant.name))
                pos = None
    return trades, pos


def _close(pair, bars, pos: _Position, out: float, how: str, rates, markup_pct: float, when, name: str) -> Trade:
    pip = pip_size(pair)
    opened = bars[pos.start].time
    carry = (rates.differential(pair, opened) if rates else None) or 0.0
    days = max((when - opened).total_seconds() / 86400, 0.0)
    total_pips = 0.0
    for fill in pos.fills:
        # Financing from the first entry: close enough for units added later.
        total_pips += (out - fill) * pos.side / pip + fill * (carry * pos.side - markup_pct) / 100 * days / 365 / pip
    risk_pips = STOP_N * pos.n0 / pip
    return Trade(
        pair, name, "long" if pos.side > 0 else "short", opened, when,
        round(pos.fills[0], 5), round(out, 5), round(risk_pips, 1), round(pos.spread_pips, 2),
        round(total_pips, 2), round(total_pips / risk_pips, 4), f"{how} ({len(pos.fills)} units)",
    )


def live_side(pair: str, bars, variant: Variant, intraday=None) -> dict | None:
    """Where the variant stands at the end of `bars`: the open position's side, size and stop, or None."""
    _, pos = simulate(pair, bars, variant, intraday=intraday)
    if pos is None:
        return None
    return {"side": "long" if pos.side > 0 else "short", "units": len(pos.fills), "stop": round(pos.stop, 5),
            "since": bars[pos.start].time.date().isoformat(), "n_pips": round(pos.n0 / pip_size(pair), 1)}
