"""The ML researcher's model: a regularised logistic regression on daily features, tested strictly out of sample.

Hypothesis H2 in research/hypotheses.yaml. The same `features` function feeds the backtest and, only if
the model ever passes the bar, the live quant seat, so what was tested is what runs.

How it stays honest:
- Walk-forward refits. The model is refitted every 1 January on earlier years only, with a 5-day embargo
  so no training label overlaps the year it trades. Every trade is out of sample.
- A control. The same model is trained on shuffled labels; real skill must beat that noise.
- Real costs, the same as the Turtle study: typical spread both ways, slippage, financing.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np

from .backtest import Trade
from .models import pip_size
from .turtle import daily_spreads, wilder_n

FEATURES = ["mom_20", "mom_60", "mom_120", "carry", "range_pos_20", "vol_regime"]
HOLD_DAYS = 5
LONG_ABOVE, SHORT_BELOW = 0.55, 0.45
L2 = 1.0
WARMUP = 260  # days of history the features need
STOP_N = 2.0


def features(bars, carry: list[float | None]) -> np.ndarray:
    """One row per day, known at that day's close. Rows without enough history are NaN."""
    c = np.array([b.close for b in bars], dtype=float)
    hi = np.array([b.high for b in bars], dtype=float)
    lo = np.array([b.low for b in bars], dtype=float)
    n = len(c)
    r = np.diff(np.log(c), prepend=np.nan)
    out = np.full((n, len(FEATURES)), np.nan)
    for t in range(WARMUP, n):
        vol60 = np.std(r[t - 59:t + 1], ddof=1)
        vol20 = np.std(r[t - 19:t + 1], ddof=1)
        vol250 = np.std(r[t - 249:t + 1], ddof=1)
        if not vol60 > 0:
            continue
        moms = [math.log(c[t] / c[t - k]) / (vol60 * math.sqrt(k)) for k in (20, 60, 120)]
        h20, l20 = hi[t - 19:t + 1].max(), lo[t - 19:t + 1].min()
        pos = (c[t] - l20) / (h20 - l20) - 0.5 if h20 > l20 else 0.0
        out[t] = [*moms, carry[t] if carry[t] is not None else 0.0, pos, vol20 / vol250 - 1 if vol250 > 0 else 0.0]
    return out


def fit(x: np.ndarray, y: np.ndarray, l2: float = L2, steps: int = 25) -> dict:
    """L2-regularised logistic regression by Newton's method, on standardised features."""
    mean, std = x.mean(axis=0), x.std(axis=0)
    std[std == 0] = 1.0
    z = np.column_stack([np.ones(len(x)), (x - mean) / std])
    w = np.zeros(z.shape[1])
    penalty = l2 * np.eye(z.shape[1])
    penalty[0, 0] = 0.0  # the intercept isn't shrunk
    for _ in range(steps):
        p = 1 / (1 + np.exp(-z @ w))
        grad = z.T @ (p - y) + penalty @ w
        hess = (z * (p * (1 - p))[:, None]).T @ z + penalty
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return {"w": w.tolist(), "mean": mean.tolist(), "std": std.tolist()}


def predict(model: dict, x: np.ndarray) -> np.ndarray:
    z = (x - np.array(model["mean"])) / np.array(model["std"])
    w = np.array(model["w"])
    return 1 / (1 + np.exp(-(w[0] + z @ w[1:])))


def _labels(bars) -> np.ndarray:
    """1 if the mid price rose from the next day's open to the close HOLD_DAYS days later, else 0."""
    n = len(bars)
    y = np.full(n, np.nan)
    for t in range(n - HOLD_DAYS):
        y[t] = float(bars[t + HOLD_DAYS].close > bars[t + 1].open)
    return y


def _carry(pair: str, bars, rates) -> list[float | None]:
    return [rates.differential(pair, b.time + timedelta(days=1)) if rates else None for b in bars]


def panel(pairs_bars: dict[str, tuple], rates) -> dict[str, dict]:
    """Features, labels and costs per pair. pairs_bars maps pair -> (daily bars, 4-hour bars)."""
    out = {}
    for pair, (bars, h4) in pairs_bars.items():
        carry = _carry(pair, bars, rates)
        out[pair] = {"bars": bars, "x": features(bars, carry), "y": _labels(bars), "carry": carry,
                     "spread": daily_spreads(bars, h4), "n": wilder_n(bars)}
    return out


def walk_forward_models(data: dict[str, dict], first_year: int, last_year: int, shuffle_seed: int | None = None):
    """A model per year, each fitted on the days whose labels were known before 1 January minus the embargo."""
    models = {}
    rng = np.random.default_rng(shuffle_seed) if shuffle_seed is not None else None
    for year in range(first_year, last_year + 1):
        cutoff = datetime(year, 1, 1, tzinfo=timezone.utc) - timedelta(days=HOLD_DAYS + 2)
        xs, ys = [], []
        for d in data.values():
            times = [b.time for b in d["bars"]]
            keep = np.array([t < cutoff for t in times]) & ~np.isnan(d["x"]).any(axis=1) & ~np.isnan(d["y"])
            xs.append(d["x"][keep])
            ys.append(d["y"][keep])
        x, y = np.concatenate(xs), np.concatenate(ys)
        if len(y) < 500:
            continue
        if rng is not None:
            y = rng.permutation(y)
        models[year] = fit(x, y)
    return models


def trades(data: dict[str, dict], models: dict[int, dict], slippage_pips: float = 0.2, markup_pct: float = 0.8,
           name: str = "ml_logit") -> list[Trade]:
    """Trade each pair with the model of the year: one position at a time, HOLD_DAYS days or a 2N stop."""
    out = []
    for pair, d in data.items():
        bars, x, pip = d["bars"], d["x"], pip_size(pair)
        slip = slippage_pips * pip
        probs = np.full(len(bars), np.nan)
        for year, model in models.items():
            rows = np.array([b.time.year == year for b in bars]) & ~np.isnan(x).any(axis=1)
            if rows.any():
                probs[rows] = predict(model, x[rows])
        t = 0
        while t < len(bars) - HOLD_DAYS - 1:
            p = probs[t]
            n = d["n"][t]
            if np.isnan(p) or n is None or not (p > LONG_ABOVE or p < SHORT_BELOW):
                t += 1
                continue
            side = 1 if p > LONG_ABOVE else -1
            spread = d["spread"][t]
            mid_in = bars[t + 1].open
            entry = mid_in + side * (spread / 2 + slip)
            stop = mid_in - side * STOP_N * n
            exit_mid, how, k = None, "time", t + HOLD_DAYS
            for j in range(t + 1, t + HOLD_DAYS + 1):
                b = bars[j]
                if (b.low <= stop) if side > 0 else (b.high >= stop):
                    # After the entry day, a gap through the stop fills at the open.
                    exit_mid = stop if j == t + 1 else (min(stop, b.open) if side > 0 else max(stop, b.open))
                    how, k = "stop", j
                    break
            if exit_mid is None:
                exit_mid = bars[k].close
            out_price = exit_mid - side * (d["spread"][k] / 2 + slip)
            days = (bars[k].time - bars[t + 1].time).total_seconds() / 86400 + 1
            carry = d["carry"][t] or 0.0
            pips = (out_price - entry) * side / pip + entry * (carry * side - markup_pct) / 100 * days / 365 / pip
            risk_pips = STOP_N * n / pip
            out.append(Trade(pair, name, "long" if side > 0 else "short", bars[t + 1].time,
                             bars[k].time + timedelta(days=1), round(entry, 5), round(out_price, 5),
                             round(risk_pips, 1), round(spread / pip, 2), round(pips, 2),
                             round(pips / risk_pips, 4), how))
            t = k + 1
    return sorted(out, key=lambda tr: tr.entry_time)


def live_side(bars, carry_pct: float | None, model: dict) -> dict | None:
    """The promoted model's call on today's features (needs WARMUP+ daily candles), or None."""
    if len(bars) <= WARMUP:
        return None
    x = features(bars, [carry_pct] * len(bars))[-1:]
    if np.isnan(x).any():
        return None
    p = float(predict(model, x)[0])
    side = "long" if p > LONG_ABOVE else "short" if p < SHORT_BELOW else None
    return {"side": side, "probability_up": round(p, 3)} if side else None
