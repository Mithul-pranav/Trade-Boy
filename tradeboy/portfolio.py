"""The risk quant: portfolio risk measured in code, and an independent sign-off on every new trade.

The Claude risk manager judges; this measures. Before each trade it treats the proposed order as if it
were already filled alongside every open trade, then checks three things:

- Correlated risk. Pairs that move together are one bet, not two. The portfolio's risk at the stops is
  sqrt(r' C r) over signed risk amounts r and the measured 60-day correlations C of daily returns,
  and must stay within `max_correlated_risk_pct` of the budget.
- Stacked currency exposure. Long EUR_USD and long GBP_USD are both short USD. Net risk on any one
  currency must stay within `max_currency_risk_pct`.
- Volatility regime. When the pair's daily ATR is well above its recent median, gaps through stops get
  likelier, so size shrinks in proportion (never below half).

Each check gives the largest share of normal size it allows; the smallest wins. The risk quant can only
shrink a trade, and the hard limits in risk.py still apply on top.
"""

from __future__ import annotations

import math
import statistics

from .market import atr
from .models import AccountState, currencies

CORR_DAYS = 60
MIN_SHARE = 0.1  # every debate still ends in an order; this is the smallest one


def returns(candles) -> list[float]:
    closes = [c.close for c in candles]
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def correlation(a: list[float], b: list[float], days: int = CORR_DAYS) -> float:
    n = min(len(a), len(b), days)
    if n < 10:
        return 1.0  # unknown is treated as moving together: the cautious assumption
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 1.0


def correlation_matrix(daily: dict[str, list], days: int = CORR_DAYS) -> dict[str, dict[str, float]]:
    rets = {p: returns(c) for p, c in daily.items()}
    return {p: {q: (1.0 if p == q else round(correlation(rets[p], rets[q], days), 2)) for q in rets} for p in rets}


def currency_exposure(positions: list[tuple[str, int, float]]) -> dict[str, float]:
    """Net signed risk per currency. positions: (instrument, +1 long / -1 short, risk amount)."""
    out: dict[str, float] = {}
    for instrument, side, risk in positions:
        base, quote = currencies(instrument)
        out[base] = out.get(base, 0.0) + side * risk
        out[quote] = out.get(quote, 0.0) - side * risk
    return out


def correlated_risk(positions: list[tuple[str, int, float]], corr: dict[str, dict[str, float]]) -> float:
    total = 0.0
    for p, sp, rp in positions:
        for q, sq, rq in positions:
            total += sp * sq * rp * rq * corr.get(p, {}).get(q, 1.0)
    return math.sqrt(max(total, 0.0))


def _largest_share(existing: list[tuple[str, int, float]], new: tuple[str, int], normal: float,
                   corr: dict, cap: float) -> float:
    """Largest f in [0, 1] with the correlated risk of existing + f * normal on `new` within cap."""
    a = correlated_risk(existing, corr) ** 2
    b = sum(new[1] * s * r * corr.get(new[0], {}).get(p, 1.0) for p, s, r in existing)
    # (f * normal)^2 + 2 * f * normal * b + a <= cap^2
    disc = b * b - (a - cap * cap)
    if disc < 0:
        return 0.0
    high = (-b + math.sqrt(disc)) / normal
    return max(0.0, min(1.0, high))


class RiskQuant:
    def __init__(self, risk_per_trade_pct: float, max_correlated_risk_pct: float = 1.0,
                 max_currency_risk_pct: float = 1.0, vol_spike: float = 1.25):
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_correlated_risk_pct = max_correlated_risk_pct
        self.max_currency_risk_pct = max_currency_risk_pct
        self.vol_spike = vol_spike

    @classmethod
    def from_settings(cls, settings) -> "RiskQuant":
        cfg = (settings.team or {}).get("risk_quant") or {}
        return cls(settings.risk.risk_per_trade_pct, **cfg)

    def assess(self, instrument: str, account: AccountState, daily: dict[str, list]) -> dict:
        """The sign-off for a new trade on `instrument`, each side treated as if already filled.

        daily: daily candles for the instrument and every pair with an open trade.
        """
        nav = max(account.nav, 0.0)
        normal = nav * self.risk_per_trade_pct / 100
        existing = [(t.instrument, 1 if t.units > 0 else -1, t.risk_amount) for t in account.open_trades]
        corr = correlation_matrix(daily)
        corr_cap, cur_cap = nav * self.max_correlated_risk_pct / 100, nav * self.max_currency_risk_pct / 100
        now_exposure = currency_exposure(existing)

        ratio = None
        candles = daily.get(instrument) or []
        if len(candles) > 40:
            atrs = [atr(candles[: i + 1]) for i in range(20, len(candles))]
            median = statistics.median(atrs)
            ratio = atrs[-1] / median if median > 0 else None
        vol_share = 1.0 if ratio is None else max(0.5, min(1.0, self.vol_spike / ratio))

        sides = {}
        for name, side in (("long", 1), ("short", -1)):
            if normal <= 0:
                sides[name] = {"max_share": MIN_SHARE, "binding": "no budget left"}
                continue
            shares = {"correlated risk": _largest_share(existing, (instrument, side), normal, corr, corr_cap)}
            for cur, sign in zip(currencies(instrument), (side, -side)):
                held = now_exposure.get(cur, 0.0)
                room = cur_cap - abs(held) if held * sign >= 0 else cur_cap + abs(held)
                shares[f"{cur} exposure"] = max(0.0, min(1.0, room / normal))
            shares["volatility regime"] = vol_share
            binding = min(shares, key=shares.get)
            share = shares[binding]
            after = existing + [(instrument, side, normal * max(share, MIN_SHARE))]
            sides[name] = {
                "max_share": round(max(share, MIN_SHARE), 3),
                "binding": binding if share < 1.0 else "none",
                "over_limits_even_at_minimum": share < MIN_SHARE,
                "correlated_risk_after_pct": round(100 * correlated_risk(after, corr) / nav, 2) if nav else None,
                "currency_exposure_after_pct": {c: round(100 * v / nav, 2) for c, v in currency_exposure(after).items()
                                                if abs(v) > 1e-9} if nav else {},
            }
        open_pairs = sorted({p for p, _, _ in existing})
        return {
            "limits_pct_of_budget": {"correlated_risk": self.max_correlated_risk_pct,
                                     "per_currency": self.max_currency_risk_pct},
            "correlation_with_open_trades_60d": {p: corr.get(instrument, {}).get(p) for p in open_pairs},
            "currency_exposure_now_pct": {c: round(100 * v / nav, 2) for c, v in now_exposure.items()
                                          if abs(v) > 1e-9} if nav else {},
            "correlated_risk_now_pct": round(100 * correlated_risk(existing, corr) / nav, 2) if nav else None,
            "daily_atr_vs_median": round(ratio, 2) if ratio else None,
            "worst_case_if_every_stop_hits_pct": round(100 * sum(r for _, _, r in existing) / nav, 2) if nav else None,
            "sides": sides,
        }
