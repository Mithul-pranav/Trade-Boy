"""Research backtest of slow, macro-driven forex strategies: weekly positions on the 7 majors, 2005 onwards.

PR #7 found no edge in hourly setups. This module asks the same question of slower ideas that suit a few
debates a week: interest-rate carry, carry with a trend filter, momentum in short-rate and 10-year yield
spreads, central bank rate moves, long-horizon value, a mix of carry, trend and value, and a ridge
regression on all of those features.

How it stays honest (same bar as the hourly backtest):
- Real costs. Positions change once a week at the first 4h candle from Monday 09:00 UTC, paying half of
  that candle's real bid/ask spread plus 0.2 pips of slippage on every unit traded. Overnight financing is
  the rate gap the position earns or pays, minus OANDA's ~0.8% a year markup.
- No look-ahead. A monthly rate is only used from the first day of the month after next (when it has
  surely been published); daily policy rates from the next day; prices from completed daily candles.
- Walk-forward. Every 6 months, strategies are chosen on the years before and judged on the 6 months after.
  The ridge regression is refitted the same way. The last 12 months are a holdout used once at the end.
- No Claude. All statistics are computed here, in code.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import history
from .config import ROOT
from .models import pip_size

MACRO_DIR = ROOT / "data" / "macro"
PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD"]
CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]
START = datetime(2004, 1, 1, tzinfo=timezone.utc)
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# Monthly OECD series on FRED. Germany's 10-year yield stands in for the euro area's.
SHORT_3M = {c: f"IR3TIB01{k}M156N" for c, k in
            zip(CURRENCIES, ["US", "EZ", "GB", "JP", "AU", "CA", "CH", "NZ"])}
LONG_10Y = {c: f"IRLTLT01{k}M156N" for c, k in
            zip(CURRENCIES, ["US", "DE", "GB", "JP", "AU", "CA", "CH", "NZ"])}
# Daily policy rates on FRED (the Fed's target moved from a single rate to a range in December 2008).
FRED_POLICY = {"USD": ["DFEDTAR", "DFEDTARU"], "EUR": ["ECBDFR"]}
RBA_CHANGES = "https://www.rba.gov.au/statistics/tables/csv/a2-data.csv"
BOE_RATE = "https://www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp"
BOC_RATE = "https://www.bankofcanada.ca/valet/observations/V39079/csv"
# A 3-month rate this much older than the moment it's needed is replaced by the daily policy rate, where
# one is available (the OECD stopped updating some euro and sterling series in early 2026).
STALE_DAYS = 100


# -- downloads ---------------------------------------------------------------------------------------------


def download(client, folder: Path = MACRO_DIR, http=None) -> None:
    """Daily and 4h OANDA candles from 2004, FRED rates and yields, and central bank rate decisions."""
    import httpx

    http = http or httpx.Client(timeout=60, follow_redirects=True,
                                headers={"User-Agent": "Trade-Boy/0.2 (research)"})
    for pair in PAIRS:
        for granularity in ("D", "H4"):
            history.download(client, pair, granularity, START, folder=folder)
    for series in [*SHORT_3M.values(), *LONG_10Y.values(), *(s for ids in FRED_POLICY.values() for s in ids)]:
        resp = http.get(FRED_CSV, params={"id": series})
        resp.raise_for_status()
        (folder / f"fred_{series}.csv").write_text(resp.text)
    _save_policy(folder / "policy_AUD.csv", _parse_rba(http.get(RBA_CHANGES).text))
    _save_policy(folder / "policy_GBP.csv", _parse_boe(http.get(BOE_RATE).text))
    resp = http.get(BOC_RATE, params={"start_date": "2000-01-01"})
    _save_policy(folder / "policy_CAD.csv", _parse_boc(resp.text))


def _save_policy(path: Path, rows: list[tuple[str, float]]) -> None:
    if not rows:
        raise ValueError(f"no policy rate rows for {path.name}")
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "rate_pct"])
        w.writerows(sorted(rows))


def _parse_rba(text: str) -> list[tuple[str, float]]:
    """RBA table A2: one row per cash rate decision. Ranges ("17.00 to 17.50") are read as their midpoint."""
    rows = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 3 or not re.match(r"\d{2}-[A-Za-z]{3}-\d{4}$", r[0].strip()) or not r[2].strip():
            continue
        nums = [float(x) for x in re.findall(r"-?\d+\.?\d*", r[2])]
        rows.append((datetime.strptime(r[0].strip(), "%d-%b-%Y").date().isoformat(), sum(nums) / len(nums)))
    return rows


def _parse_boe(html: str) -> list[tuple[str, float]]:
    cells = re.findall(r"<td[^>]*>\s*([^<]+?)\s*</td>", html)
    rows = []
    for day, rate in zip(cells[::2], cells[1::2]):
        try:
            rows.append((datetime.strptime(day, "%d %b %y").date().isoformat(), float(rate)))
        except ValueError:
            continue
    return rows


def _parse_boc(text: str) -> list[tuple[str, float]]:
    rows, prev = [], None
    for r in csv.reader(io.StringIO(text)):
        if len(r) == 2 and re.match(r"\d{4}-\d{2}-\d{2}$", r[0]) and r[1]:
            rate = float(r[1])
            if rate != prev:  # keep only the days the target changed
                rows.append((r[0], rate))
            prev = rate
    return rows


# -- rates as they were known at each moment ---------------------------------------------------------------


def _fred(folder: Path, series: str) -> pd.Series:
    df = pd.read_csv(folder / f"fred_{series}.csv")
    df.columns = ["date", "value"]
    df = df[pd.to_numeric(df["value"], errors="coerce").notna()]
    return pd.Series(df["value"].astype(float).values, index=pd.to_datetime(df["date"], utc=True))


def _known_monthly(s: pd.Series) -> pd.Series:
    """A monthly average for month M is used from the first day of month M+2."""
    return pd.Series(s.values, index=s.index + pd.DateOffset(months=2))


def policy_rates(folder: Path = MACRO_DIR) -> dict[str, pd.Series]:
    """Each central bank's policy rate by the day it was set, where a daily record is available."""
    out = {}
    for cur, ids in FRED_POLICY.items():
        parts = [_fred(folder, i) for i in ids]
        s = pd.concat(parts).sort_index()
        out[cur] = s[~s.index.duplicated(keep="last")]
    for cur in ("AUD", "GBP", "CAD"):
        df = pd.read_csv(folder / f"policy_{cur}.csv")
        out[cur] = pd.Series(df["rate_pct"].values, index=pd.to_datetime(df["date"], utc=True)).sort_index()
    return out


def rate_decisions(policy: dict[str, pd.Series]) -> pd.DataFrame:
    """Every change in a policy rate since 2004: date, currency, and +1 for a hike or -1 for a cut."""
    rows = []
    for cur, s in policy.items():
        s = s[~s.index.duplicated(keep="last")]
        change = s.diff()
        for day, d in change[(change.abs() > 1e-9) & (s.index >= START)].items():
            rows.append({"date": day, "currency": cur, "direction": int(np.sign(d)), "change": round(float(d), 3)})
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def known_rates(folder: Path, kind: str, times: pd.DatetimeIndex) -> pd.DataFrame:
    """Each currency's 3-month ("short") or 10-year ("long") rate as it was known at each of `times`."""
    ids = SHORT_3M if kind == "short" else LONG_10Y
    policy = policy_rates(folder) if kind == "short" else {}
    out = {}
    for cur, sid in ids.items():
        monthly = _known_monthly(_fred(folder, sid)).sort_index()
        pos = monthly.index.searchsorted(times, side="right") - 1
        vals = np.where(pos >= 0, monthly.values[np.clip(pos, 0, None)], np.nan)
        age = np.where(pos >= 0, (times - monthly.index[np.clip(pos, 0, None)]).days, 10**6)
        if cur in policy:
            p = policy[cur]
            ppos = (p.index + pd.Timedelta(days=1)).searchsorted(times, side="right") - 1
            pvals = np.where(ppos >= 0, p.values[np.clip(ppos, 0, None)], np.nan)
            vals = np.where((age > STALE_DAYS) & ~np.isnan(pvals), pvals, vals)
        out[cur] = vals
    return pd.DataFrame(out, index=times)


def differential(rates: pd.DataFrame, pair: str) -> pd.Series:
    base, quote = pair.split("_")
    return rates[base] - rates[quote]


# -- the weekly panel --------------------------------------------------------------------------------------


def _frame(bars) -> pd.DataFrame:
    return pd.DataFrame({
        "time": [b.time for b in bars], "close": [b.close for b in bars],
        "bid_open": [b.bid_open for b in bars], "ask_open": [b.ask_open for b in bars],
    })


def weekly_pair(pair: str, folder: Path = MACRO_DIR) -> pd.DataFrame:
    """One row per week: the execution candle (first 4h candle from Monday 09:00 UTC) and what was known then.

    Columns: time, mid, spread (as a fraction of price), days to the next row, ret (mid to next row's mid),
    vol (annualised, last 60 daily closes), ret_13w/26w/52w, value (log price minus its 5-year average).
    """
    h4 = _frame(history.load(pair, "H4", folder))
    daily = _frame(history.load(pair, "D", folder))
    daily["done"] = daily["time"] + pd.Timedelta(days=1)
    first = h4["time"].min().normalize()
    mondays = pd.date_range(first - pd.Timedelta(days=first.weekday()), h4["time"].max(), freq="7D") + \
        pd.Timedelta(hours=9)
    idx = h4["time"].searchsorted(mondays)
    keep = idx < len(h4)
    idx, mondays = idx[keep], mondays[keep]
    keep = (h4["time"].values[idx] - mondays.values) <= np.timedelta64(2, "D")  # skip weeks with no Monday/Tuesday trading
    ex = h4.iloc[idx[keep]].reset_index(drop=True)
    out = pd.DataFrame({"time": ex["time"], "mid": (ex["bid_open"] + ex["ask_open"]) / 2})
    out["spread"] = (ex["ask_open"] - ex["bid_open"]) / out["mid"]
    out["ret"] = out["mid"].shift(-1) / out["mid"] - 1
    out["days"] = (out["time"].shift(-1) - out["time"]).dt.total_seconds() / 86400

    closes = daily["close"].values
    logret = np.diff(np.log(closes), prepend=np.nan)
    k = daily["done"].searchsorted(out["time"], side="right") - 1  # last completed daily candle
    logc = np.log(closes)
    cum = np.concatenate([[0.0], np.cumsum(logc)])

    def back(n):
        return np.where(k - n >= 0, np.exp(logc[k] - logc[np.clip(k - n, 0, None)]) - 1, np.nan)

    vol = pd.Series(logret).rolling(60).std().values * math.sqrt(252)
    out["vol"] = np.where(k >= 0, vol[np.clip(k, 0, None)], np.nan)
    for weeks in (4, 13, 26, 52):
        out[f"ret_{weeks}w"] = back(5 * weeks)
    n5 = 5 * 252
    avg5 = np.where(k + 1 - n5 >= 0, (cum[k + 1] - cum[np.clip(k + 1 - n5, 0, None)]) / n5, np.nan)
    out["value"] = np.where(k >= 0, logc[np.clip(k, 0, None)] - avg5, np.nan)
    out["pair"] = pair
    return out.dropna(subset=["ret"]).reset_index(drop=True)


def build_panel(folder: Path = MACRO_DIR, pairs: list[str] = PAIRS) -> pd.DataFrame:
    """All pairs' weekly rows plus the rate features known at each execution time."""
    frames = []
    for pair in pairs:
        w = weekly_pair(pair, folder)
        times = pd.DatetimeIndex(w["time"])
        short, long = known_rates(folder, "short", times), known_rates(folder, "long", times)
        w["carry"] = differential(short, pair).values
        w["yield_gap"] = differential(long, pair).values
        for col in ("carry", "yield_gap"):
            for weeks in (13, 26):
                w[f"{col}_chg_{weeks}w"] = w[col] - w[col].shift(weeks)
        frames.append(w)
    return pd.concat(frames, ignore_index=True)


# -- strategies --------------------------------------------------------------------------------------------


def _sign(x, dead: float = 0.0):
    s = np.sign(x)
    return np.where(np.abs(x) > dead, s, 0.0)


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    always_in: bool  # takes a side in every week (fits the desk's "every debate ends long or short" rule)


RULES = [
    Rule("carry", "Carry: hold the higher-yielding currency (3-month rates)", True),
    Rule("carry_trend", "Carry, only when the 26-week trend agrees (flat otherwise)", False),
    Rule("rate_mom_13w", "Short-rate momentum: side with the currency whose 3-month rate rose more over 13 weeks", False),
    Rule("rate_mom_26w", "Short-rate momentum over 26 weeks", False),
    Rule("yield_mom_13w", "10-year yield spread momentum over 13 weeks", False),
    Rule("yield_level", "10-year yield gap: hold the currency with the higher 10-year yield", True),
    Rule("trend_13w", "Price trend: direction of the last 13 weeks", True),
    Rule("trend_26w", "Price trend: direction of the last 26 weeks", True),
    Rule("trend_52w", "Price trend: direction of the last 52 weeks", True),
    Rule("value_5y", "Value: bet on a return toward the 5-year average price", True),
    Rule("policy_cycle", "Policy cycle: side with the central bank whose last rate move was a hike", False),
    Rule("carry_trend_value", "Mix: majority vote of carry, 26-week trend and value", False),
]
RULES_BY_NAME = {r.name: r for r in RULES}


def positions(panel: pd.DataFrame, rule: str, policy_dir: pd.DataFrame | None = None) -> np.ndarray:
    """+1 long the pair, -1 short, 0 flat, for every row of the panel."""
    p = panel
    if rule == "carry":
        return _sign(p["carry"])
    if rule == "carry_trend":
        c, t = _sign(p["carry"]), _sign(p["ret_26w"])
        return np.where(c == t, c, 0.0)
    if rule == "rate_mom_13w":
        return _sign(p["carry_chg_13w"], 0.10)
    if rule == "rate_mom_26w":
        return _sign(p["carry_chg_26w"], 0.10)
    if rule == "yield_mom_13w":
        return _sign(p["yield_gap_chg_13w"], 0.10)
    if rule == "yield_level":
        return _sign(p["yield_gap"])
    if rule.startswith("trend_"):
        return _sign(p[f"ret_{rule.split('_')[1]}"])
    if rule == "value_5y":
        return -_sign(p["value"])
    if rule == "policy_cycle":
        if policy_dir is None:
            return np.zeros(len(p))
        return policy_dir.reindex(p.index).fillna(0.0).values
    if rule == "carry_trend_value":
        votes = _sign(p["carry"]) + _sign(p["ret_26w"]) - _sign(p["value"])
        return _sign(votes)
    raise KeyError(rule)


def policy_direction(panel: pd.DataFrame, decisions: pd.DataFrame, months: int = 6) -> pd.Series:
    """For each row: +1 if the base currency's bank last moved up (within `months`) and the quote's did not,
    and so on; +1 means long the pair. Banks without a daily record count as no recent move."""
    def last_move(cur: str, t: pd.Timestamp) -> float:
        d = decisions[(decisions["currency"] == cur) & (decisions["date"] + pd.Timedelta(days=1) <= t)
                      & (decisions["date"] > t - pd.DateOffset(months=months))]
        return float(d["direction"].iloc[-1]) if len(d) else 0.0

    out = []
    for i, row in panel.iterrows():
        base, quote = row["pair"].split("_")
        out.append(np.sign(last_move(base, row["time"]) - last_move(quote, row["time"])))
    return pd.Series(out, index=panel.index)


# -- returns after costs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Costs:
    slippage_pips: float = 0.2  # per side, on top of half the real spread
    spread_multiple: float = 1.0  # 2.0 for the higher-cost check
    financing_markup_pct: float = 0.8  # OANDA's yearly charge on top of the rate gap, each direction
    pair_vol_target: float = 0.10  # each pair's position is sized to this annual volatility
    max_leverage: float = 4.0  # per pair, as a multiple of the notional that would be 1.0 at the target


def pair_returns(panel: pd.DataFrame, pos: np.ndarray, costs: Costs = Costs()) -> pd.DataFrame:
    """Weekly net return of each pair's position (as a fraction of the capital set aside for it)."""
    p = panel.assign(signal=pos)
    rows = []
    for pair, g in p.groupby("pair", sort=False):
        g = g.sort_values("time")
        size = np.clip(costs.pair_vol_target / g["vol"].values, 0, costs.max_leverage)
        w = np.nan_to_num(g["signal"].values * size)
        prev = np.concatenate([[0.0], w[:-1]])
        slip = costs.slippage_pips * pip_size(pair) / g["mid"].values
        trade_cost = np.abs(w - prev) * (g["spread"].values * costs.spread_multiple / 2 + slip)
        carry = np.nan_to_num(g["carry"].values)
        financing = (w * carry - np.abs(w) * costs.financing_markup_pct) / 100 * g["days"].values / 365
        rows.append(pd.DataFrame({
            "time": g["time"].values, "pair": pair, "weight": w, "gross": w * g["ret"].values,
            "financing": financing, "cost": trade_cost, "net": w * g["ret"].values + financing - trade_cost,
        }))
    return pd.concat(rows, ignore_index=True)


def portfolio(pair_rets: pd.DataFrame, n_pairs: int = len(PAIRS)) -> pd.DataFrame:
    """Equal capital in each of the 7 pairs: the week's return is the pairs' total divided by 7."""
    t = pd.to_datetime(pair_rets["time"], utc=True).dt.normalize()
    t = t - pd.to_timedelta(t.dt.weekday, unit="D")  # the Monday of each row's week
    g = pair_rets.assign(week=t).groupby("week")
    out = g[["net", "gross", "financing", "cost"]].sum() / n_pairs
    out["active"] = g["weight"].apply(lambda w: int((w != 0).sum()))
    return out


# -- statistics --------------------------------------------------------------------------------------------


def summary(weekly: pd.Series, vol_target: float = 0.10) -> dict:
    """Sharpe ratio, t-stat, and the results scaled to a portfolio run at `vol_target` annual volatility."""
    r = weekly.dropna()
    n = len(r)
    if n < 10 or r.std() == 0:
        return {"weeks": n, "sharpe": 0.0, "t_stat": 0.0, "ann_return_at_target": 0.0,
                "max_drawdown_at_target": 0.0, "hit_rate": 0.0}
    sd = r.std()
    scaled = r * (vol_target / (sd * math.sqrt(52)))
    equity = np.cumprod(1 + scaled.values)
    dd = 1 - equity / np.maximum.accumulate(equity)
    return {
        "weeks": n,
        "sharpe": round(float(r.mean() / sd * math.sqrt(52)), 2),
        "t_stat": round(float(r.mean() / (sd / math.sqrt(n))), 2),
        "ann_return_at_target": round(float(scaled.mean() * 52), 4),
        "max_drawdown_at_target": round(float(dd.max()), 3),
        "hit_rate": round(float((r[r != 0] > 0).mean()), 3) if (r != 0).any() else 0.0,
    }


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm-adjusted p-values: corrects for testing many strategies at once."""
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m, out, running = len(items), {}, 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def p_value(t: float) -> float:
    """One-sided p-value for a t-stat (normal approximation; fine with hundreds of weeks)."""
    return 0.5 * math.erfc(t / math.sqrt(2))


# -- ridge regression (the ML seat) ------------------------------------------------------------------------

FEATURES = ["carry", "carry_chg_13w", "carry_chg_26w", "yield_gap", "yield_gap_chg_13w",
            "ret_4w", "ret_13w", "ret_26w", "ret_52w", "value"]


def _design(panel: pd.DataFrame) -> np.ndarray:
    x = panel[FEATURES].copy()
    for col in ("ret_4w", "ret_13w", "ret_26w", "ret_52w"):
        x[col] = x[col] / panel["vol"]  # past returns in units of volatility
    return x.values.astype(float)


def ridge_positions(panel: pd.DataFrame, refits: list[pd.Timestamp], alpha: float = 50.0,
                    min_train_weeks: int = 260) -> np.ndarray:
    """Pooled ridge regression of next week's volatility-scaled return on the features, refitted at each
    date in `refits` on data strictly before it. Position = sign of the prediction. Fixed alpha, no tuning."""
    x = _design(panel)
    y = (panel["ret"] / (panel["vol"] / math.sqrt(52))).values
    t = pd.DatetimeIndex(panel["time"])
    done = t + pd.Timedelta(days=7)  # a row's outcome is known a week after it starts
    pos = np.zeros(len(panel))
    ok = ~np.isnan(x).any(axis=1) & ~np.isnan(y)
    bounds = list(refits) + [t.max() + pd.Timedelta(days=1)]
    for start, end in zip(bounds[:-1], bounds[1:]):
        train = ok & (done <= start)
        if train.sum() < min_train_weeks * len(PAIRS) * 0.5:
            continue
        mu, sd = x[train].mean(axis=0), x[train].std(axis=0)
        sd[sd == 0] = 1
        xt = (x[train] - mu) / sd
        yt = np.clip(y[train], -4, 4)
        beta = np.linalg.solve(xt.T @ xt + alpha * np.eye(xt.shape[1]), xt.T @ (yt - yt.mean()))
        test = ok & (t >= start) & (t < end)
        pos[test] = np.sign(((x[test] - mu) / sd) @ beta)
    return pos


# -- walk-forward ------------------------------------------------------------------------------------------


@dataclass
class Plan:
    first_test: datetime = datetime(2010, 1, 1, tzinfo=timezone.utc)
    holdout_start: datetime = datetime(2025, 10, 22, tzinfo=timezone.utc)
    test_months: int = 6
    min_t_stat: float = 2.0
    min_train_weeks: int = 260
    vol_target: float = 0.10
    capital_usd: float = 100.0
    debate_cost_usd: float = 0.15  # one multi-agent debate (measured 0.11 to 0.21)
    costs: Costs = field(default_factory=Costs)


def _windows(plan: Plan) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    out, start = [], pd.Timestamp(plan.first_test)
    end_all = pd.Timestamp(plan.holdout_start)
    while start < end_all:
        end = min(start + pd.DateOffset(months=plan.test_months), end_all)
        out.append((start, end))
        start = end
    return out


def walk_forward(weekly: dict[str, pd.Series], plan: Plan) -> tuple[list[dict], pd.Series]:
    """Each window trades the strategies whose t-stat over all earlier weeks (at least 5 years) was at least
    `min_t_stat`, each scaled to equal volatility on those earlier weeks. Returns windows and the
    out-of-sample weekly returns."""
    windows, oos = [], []
    for start, end in _windows(plan):
        chosen, parts = [], []
        for name, r in weekly.items():
            train = r[(r.index < start)].dropna()
            if len(train) < plan.min_train_weeks or train.std() == 0:
                continue
            s = summary(train, plan.vol_target)
            if s["t_stat"] >= plan.min_t_stat:
                chosen.append(name)
                parts.append(r[(r.index >= start) & (r.index < end)] / (train.std() * math.sqrt(52)) * plan.vol_target)
        idx = pd.date_range(start, end, freq="W-MON", tz="UTC", inclusive="left")
        if parts:
            test = pd.concat(parts, axis=1).fillna(0).mean(axis=1)
        else:
            test = pd.Series(0.0, index=idx)
        oos.append(test)
        windows.append({"start": start.date().isoformat(), "end": end.date().isoformat(), "chosen": chosen,
                        "return": round(float(test.sum()), 4)})
    return windows, pd.concat(oos).sort_index() if oos else pd.Series(dtype=float)


# -- exits, head to head on the same entries -----------------------------------------------------------------


@dataclass(frozen=True)
class ExitRule:
    name: str
    description: str
    stop_sigmas: float | None = None  # stop this many weekly price moves (1 sigma) from entry
    target_r: float | None = None  # take profit at this multiple of the stop distance
    trailing: bool = False  # the stop follows the best price reached
    max_weeks: int | None = None  # close at the weekly execution this many weeks after entry
    on_flip: bool = False  # close at the first weekly check where the signal no longer agrees
    on_policy: bool = False  # close after a central bank rate move against the trade


EXIT_RULES = [
    ExitRule("flip", "Hold until the weekly signal no longer agrees", on_flip=True),
    ExitRule("flip_stop", "Signal flip, plus a protective stop 2 weekly moves away", stop_sigmas=2.0, on_flip=True),
    ExitRule("flip_policy", "Signal flip, or a central bank rate move against the trade", on_flip=True, on_policy=True),
    ExitRule("stop_target", "Fixed stop 2 weekly moves away, target at 2x the stop, at most 13 weeks",
             stop_sigmas=2.0, target_r=2.0, max_weeks=13),
    ExitRule("trailing", "Trailing stop 2 weekly moves behind the best price, at most 26 weeks",
             stop_sigmas=2.0, trailing=True, max_weeks=26),
    ExitRule("time_4w", "Close after 4 weeks, whatever happens", max_weeks=4),
    ExitRule("time_13w", "Close after 13 weeks, whatever happens", max_weeks=13),
]
EXIT_RULES_BY_NAME = {e.name: e for e in EXIT_RULES}
# Rules whose entries the exits are compared on: the theory-driven ones that take a side most weeks.
EXIT_STUDY_RULES = ["carry", "carry_trend", "rate_mom_26w", "yield_level", "trend_26w", "policy_cycle",
                    "carry_trend_value"]


def _h4_arrays(pair: str, folder: Path) -> dict[str, np.ndarray]:
    bars = history.load(pair, "H4", folder)
    return {
        "time": pd.DatetimeIndex([b.time for b in bars]),
        "bo": np.array([b.bid_open for b in bars]), "bh": np.array([b.bid_high for b in bars]),
        "bl": np.array([b.bid_low for b in bars]), "bc": np.array([b.bid_close for b in bars]),
        "ao": np.array([b.ask_open for b in bars]), "ah": np.array([b.ask_high for b in bars]),
        "al": np.array([b.ask_low for b in bars]), "ac": np.array([b.ask_close for b in bars]),
    }


def _against(decisions: pd.DataFrame, pair: str, side: int) -> pd.DatetimeIndex:
    """When each rate move that hurts this side became known (the day after the decision)."""
    base, quote = pair.split("_")
    hurt = ((decisions["currency"] == base) & (decisions["direction"] == -side)) | \
        ((decisions["currency"] == quote) & (decisions["direction"] == side))
    return pd.DatetimeIndex(decisions.loc[hurt, "date"] + pd.Timedelta(days=1)).sort_values()


def run_exit(pair: str, h4: dict, g: pd.DataFrame, entry_row: int, side: int, ex: ExitRule,
             decisions: pd.DataFrame | None, costs: Costs = Costs()) -> dict | None:
    """One trade from the execution candle of weekly row `entry_row` until `ex` closes it, on 4h bid/ask
    candles. A stop and a target touched in the same candle count as the stop. Returns the net result on the
    capital set aside for the pair (the position is sized to the pair's volatility target)."""
    t0 = g["time"].iloc[entry_row]
    start = h4["time"].searchsorted(t0)
    if start >= len(h4["time"]) or np.isnan(g["vol"].iloc[entry_row]):
        return None
    long = side > 0
    entry = h4["ao"][start] if long else h4["bo"][start]
    sigma = g["vol"].iloc[entry_row] / math.sqrt(52) * entry  # one typical weekly move, in price
    stop = entry - side * ex.stop_sigmas * sigma if ex.stop_sigmas else None
    target = entry + side * ex.target_r * ex.stop_sigmas * sigma if ex.target_r else None
    best = entry
    weekly_at = h4["time"].searchsorted(g["time"])  # the 4h candle of each weekly execution
    checks = {int(weekly_at[k]): k for k in range(entry_row + 1, len(g))}
    bad_news = _against(decisions, pair, side) if ex.on_policy and decisions is not None else \
        pd.DatetimeIndex([], tz="UTC")
    bad_news = bad_news[bad_news > t0]
    news_at = int(h4["time"].searchsorted(bad_news[0])) if len(bad_news) else None
    price = how = None
    n = len(h4["time"])
    for j in range(start, n):
        o, hi, lo, c = (h4["bo"][j], h4["bh"][j], h4["bl"][j], h4["bc"][j]) if long else \
            (h4["ao"][j], h4["ah"][j], h4["al"][j], h4["ac"][j])
        adverse, favourable = (lo, hi) if long else (hi, lo)
        k = checks.get(j)
        if j > start and k is not None:
            if ex.max_weeks and k - entry_row >= ex.max_weeks:
                price, how = o, "time"
            elif ex.on_flip and g["signal"].iloc[k] != side:
                price, how = o, "signal"
        if price is None and news_at is not None and j >= news_at and j > start:
            price, how = o, "central bank"
        if price is None and stop is not None:
            if j > start and (o - stop) * side <= 0:
                price, how = o, "stop"
            elif (adverse - stop) * side <= 0:
                price, how = stop, "stop"
        if price is None and target is not None and (favourable - target) * side >= 0:
            price, how = target, "target"
        if price is not None:
            break
        if ex.trailing:
            best = max(best, favourable) if long else min(best, favourable)
            stop = best - side * ex.stop_sigmas * sigma
    if price is None:
        return None  # still open at the end of the data
    t1 = h4["time"][j]
    size = min(costs.pair_vol_target / g["vol"].iloc[entry_row], costs.max_leverage)
    slip = 2 * costs.slippage_pips * pip_size(pair) / entry
    days = (t1 - t0).total_seconds() / 86400
    carry = g["carry"].iloc[entry_row]
    fin = ((0.0 if np.isnan(carry) else carry) * side - costs.financing_markup_pct) / 100 * days / 365
    net = (side * (price - entry) / entry - slip + fin) * size
    return {"pair": pair, "entry_time": t0, "exit_time": t1, "side": side, "how": how, "weeks": days / 7,
            "net": net, "risk_units": net / (size * sigma / entry)}


def exit_strategy_returns(panel: pd.DataFrame, rule: str, ex: ExitRule, pdir: pd.Series,
                          decisions: pd.DataFrame, h4s: dict, costs: Costs = Costs()) -> pd.Series:
    """A tradeable version: one position per pair at a time. While flat, enter at the first weekly
    execution where the rule takes a side; hold until `ex` closes it. Each trade's result is booked in the
    week it closes; weekly portfolio return = the pairs' total divided by 7."""
    p = panel.assign(signal=positions(panel, rule, pdir))
    booked = []
    for pair, g in p.groupby("pair", sort=False):
        g = g.sort_values("time").reset_index(drop=True)
        i = 1
        while i < len(g):
            side = int(g["signal"].iloc[i])
            if side == 0 or np.isnan(g["vol"].iloc[i]):
                i += 1
                continue
            t = run_exit(pair, h4s[pair], g, i, side, ex, decisions, costs)
            if t is None:
                break
            booked.append((t["exit_time"], t["net"]))
            i = max(i + 1, int(g["time"].searchsorted(t["exit_time"])))
    if not booked:
        return pd.Series(dtype=float)
    df = pd.DataFrame(booked, columns=["time", "net"])
    week = df["time"].dt.normalize() - pd.to_timedelta(df["time"].dt.weekday, unit="D")
    weekly = df.groupby(week)["net"].sum() / len(PAIRS)
    all_weeks = pd.date_range(weekly.index.min(), weekly.index.max(), freq="W-MON")
    return weekly.reindex(all_weeks, fill_value=0.0)


def exit_study(panel: pd.DataFrame, decisions: pd.DataFrame, pdir: pd.Series, end: pd.Timestamp,
               folder: Path = MACRO_DIR, rules=EXIT_STUDY_RULES, random_seeds: int = 10,
               costs: Costs = Costs()) -> list[dict]:
    """Every exit rule applied to exactly the same entries: each week a rule's signal turns on or changes
    side. Also on random entries (a coin flip for the side, at the same number of entry weeks), to show what
    exits do with no entry edge at all. Trades may overlap; each is scored on its own."""
    rng = np.random.default_rng(7)
    h4s = {pair: _h4_arrays(pair, folder) for pair in PAIRS}
    out = []
    entry_sets = {}
    for rule in rules:
        sig = positions(panel, rule, pdir)
        entry_sets[rule] = (sig, None)
    for seed in range(random_seeds):
        entry_sets[f"random_{seed}"] = (None, rng)
    for name, (sig, r) in entry_sets.items():
        p = panel.copy()
        if sig is not None:
            p["signal"] = sig
        trades = {e.name: [] for e in EXIT_RULES}
        for pair, g in p.groupby("pair", sort=False):
            g = g.sort_values("time").reset_index(drop=True)
            g = g[g["time"] < end].reset_index(drop=True)
            if sig is None:
                n_entries = max(1, len(g) // 8)  # about one new trade every 8 weeks per pair
                rows = np.sort(r.choice(np.arange(60, len(g)), n_entries, replace=False))
                sides = r.choice([-1, 1], n_entries)
                g["signal"] = 0.0
                for row, sd in zip(rows, sides):  # the coin's side holds until the next coin
                    g.loc[row:, "signal"] = sd
                starts = list(zip(rows, sides))
            else:
                s = g["signal"].values
                starts = [(i, int(s[i])) for i in range(1, len(g)) if s[i] != 0 and s[i] != s[i - 1]]
            for i, side in starts:
                for e in EXIT_RULES:
                    t = run_exit(pair, h4s[pair], g, i, side, e, decisions, costs)
                    if t and t["exit_time"] < end:
                        trades[e.name].append(t)
        for e in EXIT_RULES:
            tr = trades[e.name]
            if not tr:
                continue
            ru = np.array([t["risk_units"] for t in tr])
            net = np.array([t["net"] for t in tr])
            out.append({"entries": name, "exit": e.name, "trades": len(tr),
                        "avg_risk_units": float(ru.mean()), "t_stat": float(ru.mean() / (ru.std(ddof=1) / math.sqrt(len(ru)))),
                        "won_pct": float(100 * (net > 0).mean()), "avg_weeks": float(np.mean([t["weeks"] for t in tr])),
                        "total_net": float(net.sum())})
    df = pd.DataFrame(out)
    rand = df[df["entries"].str.startswith("random_")]
    if len(rand):
        avg = rand.groupby("exit").agg(trades=("trades", "mean"), avg_risk_units=("avg_risk_units", "mean"),
                                       t_stat=("t_stat", "mean"), won_pct=("won_pct", "mean"),
                                       avg_weeks=("avg_weeks", "mean"), total_net=("total_net", "mean"),
                                       worst=("avg_risk_units", "min"), best=("avg_risk_units", "max")).reset_index()
        avg["entries"] = "random"
        df = pd.concat([df[~df["entries"].str.startswith("random_")], avg], ignore_index=True)
    return df.round(3).to_dict("records")


# -- the whole run -----------------------------------------------------------------------------------------


def strategy_returns(panel: pd.DataFrame, decisions: pd.DataFrame, plan: Plan, costs: Costs | None = None,
                     folder: Path = MACRO_DIR) -> dict[str, pd.Series]:
    costs = costs or plan.costs
    pdir = policy_direction(panel, decisions)
    out = {}
    for rule in RULES:
        out[rule.name] = portfolio(pair_returns(panel, positions(panel, rule.name, pdir), costs))["net"]
    h4s = {pair: _h4_arrays(pair, folder) for pair in PAIRS}
    for rule in EXIT_STUDY_RULES:
        for ex in EXIT_RULES:
            if ex.name != "flip":  # the flip exit is the weekly strategy above
                out[f"{rule}+{ex.name}"] = exit_strategy_returns(panel, rule, ex, pdir, decisions, h4s, costs)
    refits = [pd.Timestamp(s) for s, _ in _windows(plan)] + [pd.Timestamp(plan.holdout_start)]
    out["ridge"] = portfolio(pair_returns(panel, ridge_positions(panel, refits), costs))["net"]
    return out


def event_study(panel: pd.DataFrame, decisions: pd.DataFrame, holds=(1, 2, 4), costs: Costs = Costs()) -> list[dict]:
    """After each policy rate change, trade the currency in the direction of the move against the US dollar
    (for a Fed move, the dollar against all six) from the next weekly execution, for 1, 2 and 4 weeks."""
    out = []
    by_pair = {p: g.sort_values("time").reset_index(drop=True) for p, g in panel.groupby("pair")}
    for h in holds:
        rets = []
        for _, d in decisions.iterrows():
            legs = []
            for pair in [p for p in PAIRS if d["currency"] in p.split("_")]:
                g = by_pair[pair]
                k = g["time"].searchsorted(d["date"] + pd.Timedelta(days=1))
                if k + h > len(g) or np.isnan(g["vol"].iloc[k]):
                    continue
                rows = g.iloc[k:k + h]
                side = d["direction"] * (1 if pair.split("_")[0] == d["currency"] else -1)
                gross = float(np.prod(1 + side * rows["ret"].values) - 1)
                slip = costs.slippage_pips * pip_size(pair) / rows["mid"].iloc[0]
                cost = rows["spread"].iloc[0] / 2 + g["spread"].iloc[min(k + h, len(g) - 1)] / 2 + 2 * slip
                fin = (side * np.nan_to_num(rows["carry"].values) - costs.financing_markup_pct) / 100 \
                    * rows["days"].values / 365
                net = gross - cost + float(fin.sum())
                legs.append(net / (rows["vol"].iloc[0] * math.sqrt(h / 52)))  # in units of h-week volatility
            if legs and d["date"] < pd.Timestamp(datetime(2025, 10, 22, tzinfo=timezone.utc)):
                rets.append(float(np.mean(legs)))
        r = np.array(rets)
        t = float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))) if len(r) > 2 else 0.0
        out.append({"hold_weeks": h, "events": len(r), "avg_vol_units": round(float(r.mean()), 3) if len(r) else 0.0,
                    "won_pct": round(100 * float((r > 0).mean()), 1) if len(r) else 0.0, "t_stat": round(t, 2)})
    return out


def debate_economics(ann_return_at_target: float, plan: Plan) -> list[dict]:
    """What Claude debates would cost a year against what the strategy is expected to make."""
    expected = ann_return_at_target * plan.capital_usd
    designs = [
        ("One debate per pair per week (7 a week)", 7 * 52),
        ("One debate a week for the whole portfolio", 52),
        ("One debate a month for the whole portfolio", 12),
    ]
    out = []
    for label, n in designs:
        cost = n * plan.debate_cost_usd
        breakeven = cost / ann_return_at_target if ann_return_at_target > 0 else None
        out.append({"design": label, "debates_per_year": n, "api_cost_usd": round(cost, 2),
                    "expected_profit_usd": round(expected, 2), "net_usd": round(expected - cost, 2),
                    "capital_to_break_even_usd": round(breakeven) if breakeven else None})
    return out


def run(plan: Plan = Plan(), folder: Path = MACRO_DIR) -> dict:
    panel = build_panel(folder)
    decisions = rate_decisions(policy_rates(folder))
    holdout = pd.Timestamp(plan.holdout_start)
    weekly = strategy_returns(panel, decisions, plan, folder=folder)
    stressed_costs = Costs(spread_multiple=2.0, financing_markup_pct=1.5, slippage_pips=0.5)
    stressed = strategy_returns(panel, decisions, plan, stressed_costs, folder)

    dev = {k: v[v.index < holdout] for k, v in weekly.items()}
    full = {k: summary(v[v.index >= pd.Timestamp("2006-01-01", tz="UTC")], plan.vol_target) for k, v in dev.items()}
    adjusted = holm({k: p_value(s["t_stat"]) for k, s in full.items()})
    eras = [("2006-2009", "2006-01-01", "2010-01-01"), ("2010-2014", "2010-01-01", "2015-01-01"),
            ("2015-2019", "2015-01-01", "2020-01-01"), ("2020-2025", "2020-01-01", "2025-10-22")]
    by_era = {k: {e: summary(v[(v.index >= pd.Timestamp(a, tz="UTC")) & (v.index < pd.Timestamp(b, tz="UTC"))])["sharpe"]
                  for e, a, b in eras} for k, v in dev.items()}

    windows, oos = walk_forward(weekly, plan)
    oos_dev = oos[oos.index < holdout]
    _, oos_stressed = walk_forward(stressed, plan)
    oos_stats = summary(oos_dev, plan.vol_target)
    stressed_stats = summary(oos_stressed[oos_stressed.index < holdout], plan.vol_target)

    # Holdout: strategies chosen on everything before it, judged once.
    chosen = [k for k, v in dev.items() if len(v.dropna()) >= plan.min_train_weeks
              and summary(v, plan.vol_target)["t_stat"] >= plan.min_t_stat]
    hold_parts = [weekly[k][weekly[k].index >= holdout] / (dev[k].std() * math.sqrt(52)) * plan.vol_target
                  for k in chosen]
    hold = pd.concat(hold_parts, axis=1).fillna(0).mean(axis=1) if hold_parts else pd.Series(dtype=float)
    hold_return = float(hold.sum()) if len(hold) else 0.0

    positive_windows = sum(w["return"] > 0 for w in windows if w["chosen"])
    traded_windows = sum(1 for w in windows if w["chosen"])
    economics = debate_economics(oos_stats["ann_return_at_target"], plan)
    weekly_debate = economics[1]
    bar = [
        ("Out-of-sample weeks with positions", "at least 300", sum(1 for w in windows if w["chosen"]) * 26,
         sum(1 for w in windows if w["chosen"]) * 26 >= 300),
        ("Sharpe ratio after costs (out of sample)", "at least 0.5", oos_stats["sharpe"], oos_stats["sharpe"] >= 0.5),
        ("Not luck (t-stat, out of sample)", "at least 2.0", oos_stats["t_stat"], oos_stats["t_stat"] >= 2.0),
        ("Test windows that made money", "at least 60%",
         f"{round(100 * positive_windows / traded_windows) if traded_windows else 0}%",
         traded_windows > 0 and positive_windows / traded_windows >= 0.6),
        ("Survives higher costs (2x spread, 0.5 pip slippage, 1.5% financing markup)", "Sharpe above 0",
         stressed_stats["sharpe"], stressed_stats["sharpe"] > 0),
        ("Worst drop at 10% yearly volatility", "at most 20%",
         f"{round(100 * oos_stats['max_drawdown_at_target'], 1)}%", 0 < oos_stats["max_drawdown_at_target"] <= 0.20),
        ("Holdout year (never used to choose anything)", "above 0%",
         f"{round(100 * hold_return, 2)}% ({', '.join(chosen) or 'nothing chosen'})", hold_return > 0),
        ("Pays for a weekly portfolio debate on 100 USD", f"profit above {weekly_debate['api_cost_usd']} USD a year",
         f"{weekly_debate['expected_profit_usd']} USD", weekly_debate["net_usd"] > 0),
    ]
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "first_week": str(panel["time"].min().date()), "last_week": str(panel["time"].max().date()),
        "decisions": decisions, "full": full, "adjusted_p": adjusted, "by_era": by_era,
        "full_stressed": {k: summary(v[v.index < holdout], plan.vol_target) for k, v in stressed.items()},
        "windows": windows, "oos": oos_stats, "oos_stressed": stressed_stats,
        "holdout": {"chosen": chosen, "return": hold_return, "weeks": len(hold)},
        "events": event_study(panel, decisions, costs=plan.costs), "economics": economics,
        "exits": exit_study(panel, decisions, policy_direction(panel, decisions), holdout, folder, costs=plan.costs),
        "pass_bar": [{"check": c, "needs": n, "got": g, "passed": bool(p)} for c, n, g, p in bar],
        "passed": all(p for *_, p in bar), "plan": plan,
    }
