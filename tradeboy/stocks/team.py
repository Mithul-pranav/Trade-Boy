"""The quant team: five roles, all plain code, so they cost nothing in Claude credits.

- Quant researcher (research.py): tests code-only rules such as post-earnings drift with walk-forward
  windows, a holdout year, real costs and a Holm correction. Only a rule that passes is promoted to the
  quant seat below, which speaks in every debate on that stock.
- Risk quant (RiskQuant): before each trade, treats the new position as already open next to the others
  and measures correlated risk, net risk per group (semiconductors, platforms, ...) and the volatility
  regime. Its limit on size is enforced in code; it can only shrink a trade.
- Quant trader (execution_report): records the quote at decision time against the fill (slippage), the
  stop distance against the daily range, and how far each closed trade went for and against it.
- Quant developer (health_check): daily checks that every position has a live stop, nothing at the broker
  is unmanaged, time limits are not overdue, data is fresh, the account is the paper one and the spend
  caps hold. Problems go to Telegram.
- ML researcher (calibration): checks whether the agents' confidence means anything: are 70%-confident
  calls right about 70% of the time? Price-feature models are tested in research, forward only for
  anything that reads news (Claude already knows how past stories ended, so a news backtest would cheat).
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from ..config import ROOT
from ..market import atr
from .alpaca import PAPER_URL
from .market import log_returns

CORR_DAYS = 60
MIN_SHARE = 0.1  # every debate still ends in an order; this is the smallest one
PROMOTED = ROOT / "data" / "stocks_research" / "promoted.json"


# -- risk quant ---------------------------------------------------------------------------------

def correlation(a: list[float], b: list[float], days: int = CORR_DAYS) -> float:
    n = min(len(a), len(b), days)
    if n < 10:
        return 1.0  # unknown is treated as moving together: the cautious assumption
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 1.0


def correlated_risk(positions: list[tuple[str, int, float]], corr: dict) -> float:
    total = 0.0
    for p, sp, rp in positions:
        for q, sq, rq in positions:
            total += sp * sq * rp * rq * (1.0 if p == q else corr.get(p, {}).get(q, 1.0))
    return math.sqrt(max(total, 0.0))


def _largest_share(existing, new: tuple[str, int], normal: float, corr: dict, cap: float) -> float:
    """Largest f in [0, 1] with the correlated risk of existing + f * normal on `new` within cap."""
    a = correlated_risk(existing, corr) ** 2
    b = sum(new[1] * s * r * (1.0 if p == new[0] else corr.get(new[0], {}).get(p, 1.0)) for p, s, r in existing)
    disc = b * b - (a - cap * cap)
    if disc < 0:
        return 0.0
    return max(0.0, min(1.0, (-b + math.sqrt(disc)) / normal))


class RiskQuant:
    def __init__(self, risk_per_trade_pct: float, group_of, max_correlated_risk_pct: float = 3.0,
                 max_group_risk_pct: float = 2.0, vol_spike: float = 1.25):
        self.risk_per_trade_pct = risk_per_trade_pct
        self.group_of = group_of
        self.max_correlated_risk_pct = max_correlated_risk_pct
        self.max_group_risk_pct = max_group_risk_pct
        self.vol_spike = vol_spike

    @classmethod
    def from_settings(cls, settings) -> "RiskQuant":
        cfg = (settings.team or {}).get("risk_quant") or {}
        return cls(settings.risk.risk_per_trade_pct, settings.group, **cfg)

    def assess(self, symbol: str, nav: float, open_positions: list[tuple[str, int, float]], daily: dict) -> dict:
        """open_positions: (symbol, +1/-1, risk at the stop). daily: daily bars for symbol and each open one."""
        nav = max(nav, 0.0)
        normal = nav * self.risk_per_trade_pct / 100
        rets = {s: log_returns(b) for s, b in daily.items()}
        corr = {p: {q: round(correlation(rets[p], rets[q]), 2) for q in rets} for p in rets}
        corr_cap, group_cap = nav * self.max_correlated_risk_pct / 100, nav * self.max_group_risk_pct / 100
        groups: dict[str, float] = {}
        for s, side, risk in open_positions:
            groups[self.group_of(s)] = groups.get(self.group_of(s), 0.0) + side * risk
        mine = self.group_of(symbol)

        ratio = None
        bars = daily.get(symbol) or []
        if len(bars) > 40:
            atrs = [atr(bars[: i + 1]) / bars[i].close for i in range(20, len(bars))]
            median = statistics.median(atrs)
            ratio = atrs[-1] / median if median > 0 else None
        vol_share = 1.0 if ratio is None else max(0.5, min(1.0, self.vol_spike / ratio))

        sides = {}
        for name, side in (("long", 1), ("short", -1)):
            if normal <= 0:
                sides[name] = {"max_share": MIN_SHARE, "binding": "no budget left"}
                continue
            held = groups.get(mine, 0.0)
            room = group_cap - abs(held) if held * side >= 0 else group_cap + abs(held)
            shares = {
                "correlated risk": _largest_share(open_positions, (symbol, side), normal, corr, corr_cap),
                f"{mine} exposure": max(0.0, min(1.0, room / normal)),
                "volatility regime": vol_share,
            }
            binding = min(shares, key=shares.get)
            share = shares[binding]
            after = open_positions + [(symbol, side, normal * max(share, MIN_SHARE))]
            sides[name] = {
                "max_share": round(max(share, MIN_SHARE), 3),
                "binding": binding if share < 1.0 else "none",
                "over_limits_even_at_minimum": share < MIN_SHARE,
                "correlated_risk_after_pct": round(100 * correlated_risk(after, corr) / nav, 2) if nav else None,
            }
        return {
            "limits_pct_of_budget": {"correlated_risk": self.max_correlated_risk_pct, "per_group": self.max_group_risk_pct},
            "group": mine,
            "correlation_with_open_positions_60d": {p: corr.get(symbol, {}).get(p) for p, _, _ in open_positions},
            "group_risk_now_pct": {g: round(100 * v / nav, 2) for g, v in groups.items() if abs(v) > 1e-9} if nav else {},
            "correlated_risk_now_pct": round(100 * correlated_risk(open_positions, corr) / nav, 2) if nav else None,
            "daily_atr_vs_median": round(ratio, 2) if ratio else None,
            "sides": sides,
        }


# -- quant seat (what the quant researcher has promoted) ----------------------------------------

class QuantSeat:
    """Speaks only with a rule that passed research. Without one it says so plainly."""

    def __init__(self, promoted: dict | None = None):
        self.promoted = promoted or {}

    @classmethod
    def load(cls, path: Path = PROMOTED) -> "QuantSeat":
        try:
            return cls(json.loads(path.read_text()))
        except (OSError, ValueError):
            return cls()

    def view(self, symbol: str, event_kind: str, reaction: dict | None) -> dict:
        rule = self.promoted.get("post_earnings_drift")
        if not rule or event_kind != "earnings" or not reaction:
            return {"significant": False, "stance": None, "hold_days": 0,
                    "note": "No tested edge applies here: the quant researcher has not promoted a rule for this event."}
        threshold = rule["threshold_pct"]
        excess = reaction.get("excess_pct")
        if excess is None or abs(excess) < threshold:
            return {"significant": False, "stance": None, "hold_days": 0,
                    "note": f"Earnings reaction {excess}% vs the benchmark is inside the tested ±{threshold}% band."}
        return {
            "significant": True, "stance": "long" if excess > 0 else "short", "hold_days": rule["hold_days"],
            "confidence": rule.get("confidence", 55),
            "note": (f"Post-earnings drift: after reactions beyond ±{threshold}%, the stock kept drifting the same way "
                     f"over {rule['hold_days']} days: {rule['avg_r']:+.2f}R per trade after costs over "
                     f"{rule['trades']} out-of-sample trades (t = {rule['t_stat']})."),
        }


# -- quant trader -------------------------------------------------------------------------------

def slippage_bps(side: str, decision_mid: float, fill: float) -> float:
    """Cost of the fill against the mid price when the order was decided, in basis points (positive = cost)."""
    sign = 1 if side == "buy" else -1
    return round((fill - decision_mid) / decision_mid * 10_000 * sign, 1) if decision_mid else 0.0


def execution_report(journal) -> dict:
    rows = journal.db.execute(
        "SELECT key, value FROM state WHERE key LIKE 'execution:%' ORDER BY key").fetchall()
    records = [json.loads(v) for _, v in rows]
    if not records:
        return {"fills": 0}
    slips = [r["slippage_bps"] for r in records]
    stops = [r["stop_vs_atr"] for r in records if r.get("stop_vs_atr")]
    return {
        "fills": len(records),
        "avg_slippage_bps": round(sum(slips) / len(slips), 1),
        "worst_slippage_bps": max(slips),
        "median_stop_in_daily_ranges": round(statistics.median(stops), 2) if stops else None,
    }


# -- quant developer ----------------------------------------------------------------------------

def health_check(settings, broker, journal, now: datetime) -> list[str]:
    """Problems that need fixing, in plain words. An empty list means all is well."""
    problems = []
    if not PAPER_URL.startswith("https://paper-api."):
        problems.append("The broker URL is not Alpaca's paper API.")
    try:
        positions = {p.symbol: p for p in broker.positions()}
        stops = {o["symbol"] for o in broker.open_orders() if o.get("type") in ("stop", "stop_limit")}
    except Exception as exc:
        return [f"Alpaca could not be read: {exc}"]
    mine = {t["instrument"]: t for t in journal.open_trades()}
    for symbol in positions:
        if symbol not in mine:
            problems.append(f"{symbol} is held at Alpaca but the desk has no record of it, so nothing manages it.")
    for symbol, trade in mine.items():
        if symbol in positions and symbol not in stops:
            problems.append(f"{symbol} has no live stop order at Alpaca.")
        if trade["hold_until"] and now > datetime.fromisoformat(trade["hold_until"]) + timedelta(days=1):
            problems.append(f"{symbol} is more than a day past its time limit and still open.")
    last_scan = journal.get_state("last_scan")
    if last_scan and now - datetime.fromisoformat(last_scan) > timedelta(days=4):
        problems.append(f"The desk hasn't scanned since {last_scan[:16]}.")
    claude = settings.claude
    if journal.month_spend(now) > claude.monthly_budget_usd * 1.02:
        problems.append(f"Claude spend this month {journal.month_spend(now):.2f} USD is over the cap.")
    return problems


# -- ML researcher ------------------------------------------------------------------------------

def calibration(journal, buckets=((50, 60), (60, 70), (70, 80), (80, 101))) -> dict:
    """Per agent and confidence band: how often the call was right. Plus the Brier score (lower is better)."""
    rows = journal.db.execute(
        "SELECT agent, confidence, result_pips FROM agent_calls WHERE result_pips IS NOT NULL AND agent != 'head_of_desk'"
    ).fetchall()
    out: dict[str, dict] = {}
    for agent in sorted({r[0] for r in rows}):
        mine = [(c, r) for a, c, r in rows if a == agent]
        bands = {}
        for low, high in buckets:
            band = [r > 0 for c, r in mine if low <= c < high]
            if band:
                bands[f"{low}-{min(high, 100)}%"] = {"calls": len(band), "right_pct": round(100 * sum(band) / len(band))}
        brier = sum((max(min(c, 100), 0) / 100 - (r > 0)) ** 2 for c, r in mine) / len(mine)
        out[agent] = {"scored_calls": len(mine), "bands": bands, "brier": round(brier, 3),
                      "coin_flip_brier": 0.25}
    return out
