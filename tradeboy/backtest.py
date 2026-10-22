"""Backtests the desk's code-level rules and the quant seat's signals on years of OANDA history.

How it stays honest:
- Real costs. Trades enter at the next candle's open at the ask (buys) or bid (sells) and exit at the
  bid or ask, so the historical spread is paid both ways, plus extra slippage per side. A stop and a
  target touched in the same candle count as the stop (the worse case).
- Walk-forward. A strategy is only traded in a 6-month test window if it showed an edge in the 3 years
  before it. The verdict comes from the test windows alone, which it was not chosen on.
- A holdout year. The last 12 months are kept out of everything above and checked once at the end.
- No Claude. Debates cost money, so the rules are tested in code; the debate's own judgement isn't.
"""

from __future__ import annotations

import bisect
import json
import math
import pickle
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import history
from .config import ROOT
from .models import pip_size
from .quant import LOOKBACK, SIGNALS, SIGNALS_BY_NAME, flip, snapshot

OUT_DIR = ROOT / "data" / "backtest"
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Exit:
    """How a backtest trade ends: a stop from volatility, an optional target in R, and a time limit."""

    name: str
    stop_atr: float  # stop distance in multiples of the ATR named by `atr`
    atr: str  # "h1" or "d"
    target_r: float | None  # take profit at this multiple of the stop distance
    max_hours: int


EXITS = [
    # The desk's own fallback stop (1.5x the 1h ATR), a 1.5R target, and about the head's usual hold.
    Exit("intraday", 1.5, "h1", 1.5, 24),
    # Slower signals (momentum) need room: one daily ATR, a 2R target, up to a week.
    Exit("swing", 1.0, "d", 2.0, 120),
]
EXITS_BY_NAME = {e.name: e for e in EXITS}


@dataclass(frozen=True)
class Strategy:
    signal: str
    inverted: bool  # trade the opposite of what the signal says
    exit: str

    @property
    def key(self) -> str:
        return f"{self.signal}{'_inverted' if self.inverted else ''}/{self.exit}"

    @property
    def label(self) -> str:
        sig = SIGNALS_BY_NAME[self.signal].description
        return f"{'OPPOSITE of: ' if self.inverted else ''}{sig} ({self.exit} exit)"


STRATEGIES = [Strategy(s.name, inv, e.name) for s in SIGNALS for inv in (False, True) for e in EXITS]


@dataclass(frozen=True)
class Costs:
    slippage_pips: float = 0.2  # extra cost on each side, on top of the real bid/ask spread
    max_spread_pips: float = 3.0  # the desk's own limit: no entry while the spread is wider
    min_stop_pips: float = 6.0  # the desk's 5-pip minimum plus its 1-pip margin
    # Overnight financing: a position earns or pays the two currencies' interest rate gap, and OANDA
    # takes about this much a year on top in each direction (its live long and short rates, Oct 2026).
    financing_markup_pct: float = 0.8


@dataclass
class Trade:
    pair: str
    strategy: str
    side: str
    entry_time: datetime
    exit_time: datetime
    entry: float
    exit: float
    stop_pips: float
    spread_pips: float
    pips: float  # after spread and slippage
    r: float  # pips / stop_pips: +1.0 means won as much as was risked
    how: str  # "stop", "target" or "time"


# -- snapshots: what the signals saw at the close of every 1h candle --------------------------------------


def _complete_upto(bars: list, duration: timedelta) -> list[datetime]:
    return [b.time + duration for b in bars]


def build_snapshots(pair: str, folder: Path = history.HISTORY_DIR) -> tuple[list, list[tuple[int, dict]]]:
    """Every 1h close in the history, with the snapshot the live desk would have taken at that moment."""
    h1, h4, daily = (history.load(pair, g, folder) for g in ("H1", "H4", "D"))
    rates = history.ShortRates.load(folder)
    h4_done = _complete_upto(h4, history.DURATION["H4"])
    d_done = _complete_upto(daily, history.DURATION["D"])
    snaps = []
    for i in range(LOOKBACK["H1"], len(h1)):
        now = h1[i].time + HOUR
        k4 = bisect.bisect_right(h4_done, now)
        kd = bisect.bisect_right(d_done, now)
        if k4 < LOOKBACK["H4"] or kd < LOOKBACK["D"]:
            continue
        carry = rates.differential(pair, now) if rates else None
        snaps.append((i, snapshot(pair, h1[i + 1 - LOOKBACK["H1"]:i + 1], h4[k4 - LOOKBACK["H4"]:k4],
                                  daily[kd - LOOKBACK["D"]:kd], carry)))
    return h1, snaps


def _cached_snapshots(pair: str, folder: Path, cache: Path) -> tuple[list, list]:
    sources = [history.path_for(pair, "H1", folder), history.rates_path(folder)]
    path = cache / f"snapshots_{pair}.pkl"
    stamp = tuple((p.stat().st_mtime, p.stat().st_size) for p in sources if p.exists())
    if path.exists():
        with path.open("rb") as f:
            saved = pickle.load(f)
        if saved["stamp"] == stamp:
            return history.load(pair, "H1", folder), saved["snaps"]
    h1, snaps = build_snapshots(pair, folder)
    cache.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        pickle.dump({"stamp": stamp, "snaps": snaps}, f)
    return h1, snaps


# -- one strategy on one pair ------------------------------------------------------------------------------


def simulate(pair: str, h1: list, snaps: list[tuple[int, dict]], strategy: Strategy, costs: Costs) -> list[Trade]:
    """One position at a time: enter at the next candle's open whenever the strategy signals while flat."""
    rule = SIGNALS_BY_NAME[strategy.signal].rule
    ex = EXITS_BY_NAME[strategy.exit]
    pip = pip_size(pair)
    trades: list[Trade] = []
    free_from = 0  # first 1h candle index at which a new trade may be decided
    for i, snap in snaps:
        if i < free_from or i + 1 >= len(h1):
            continue
        side = rule(snap)
        if strategy.inverted:
            side = flip(side)
        if side is None:
            continue
        bar = h1[i + 1]
        spread = (bar.ask_open - bar.bid_open) / pip
        if spread > costs.max_spread_pips:
            continue
        atr_pips = snap["h1_atr_pips"] if ex.atr == "h1" else snap["d_atr_pips"]
        stop_pips = max(ex.stop_atr * atr_pips, costs.min_stop_pips)
        trade, last = _run_trade(pair, h1, i + 1, side, stop_pips, ex, costs, pip, strategy.key, spread,
                                 snap["carry_pct"])
        if trade:
            trades.append(trade)
        free_from = last
    return trades


def _run_trade(pair, h1, start, side, stop_pips, ex: Exit, costs: Costs, pip, key, spread, carry_pct):
    long = side == "long"
    sign = 1 if long else -1
    entry = h1[start].ask_open if long else h1[start].bid_open
    stop = entry - sign * stop_pips * pip
    target = entry + sign * ex.target_r * stop_pips * pip if ex.target_r else None
    entry_time = h1[start].time
    deadline = entry_time + timedelta(hours=ex.max_hours)
    for j in range(start, len(h1)):
        b = h1[j]
        # A long is closed by selling at the bid; a short by buying at the ask.
        o, hi, lo, c = (b.bid_open, b.bid_high, b.bid_low, b.bid_close) if long else \
            (b.ask_open, b.ask_high, b.ask_low, b.ask_close)
        adverse, favourable = (lo, hi) if long else (hi, lo)
        price = how = None
        if j > start and (o - stop) * sign <= 0:
            price, how = o, "stop"  # gapped through the stop: filled at the open, worse than the stop
        elif (adverse - stop) * sign <= 0:
            price, how = stop, "stop"
        elif target is not None and (favourable - target) * sign >= 0:
            price, how = target, "target"
        elif b.time + HOUR >= deadline:
            price, how = c, "time"
        if price is not None:
            pips = (price - entry) * sign / pip - 2 * costs.slippage_pips
            # Interest earned or paid while the trade was open, from the rates known at entry.
            days = (b.time + HOUR - entry_time).total_seconds() / 86400
            yearly = (carry_pct or 0.0) * sign - costs.financing_markup_pct
            pips += entry * yearly / 100 * days / 365 / pip
            return Trade(pair, key, side, entry_time, b.time + HOUR, entry, price, round(stop_pips, 1),
                         round(spread, 2), round(pips, 2), round(pips / stop_pips, 4), how), j + 1
    return None, len(h1)


def _pair_trades(args) -> list[Trade]:
    pair, folder, cache, costs, strategies = args
    h1, snaps = _cached_snapshots(pair, folder, cache)
    out = []
    for strat in strategies:
        out.extend(simulate(pair, h1, snaps, strat, costs))
    return out


def all_trades(pairs: list[str], costs: Costs, strategies=STRATEGIES, folder: Path = history.HISTORY_DIR,
               cache: Path = OUT_DIR, workers: int = 4) -> list[Trade]:
    jobs = [(p, folder, cache, costs, strategies) for p in pairs]
    if workers <= 1:
        results = map(_pair_trades, jobs)
    else:
        pool = ProcessPoolExecutor(max_workers=workers)
        results = pool.map(_pair_trades, jobs)
    trades = [t for chunk in results for t in chunk]
    return sorted(trades, key=lambda t: t.entry_time)


# -- statistics ------------------------------------------------------------------------------------------


def _month(t: datetime) -> tuple[int, int]:
    return t.year, t.month


def _months_between(start: datetime, end: datetime) -> list[tuple[int, int]]:
    out, (y, m) = [], _month(start)
    while (y, m) <= _month(end - timedelta(seconds=1)):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def t_stat(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean / math.sqrt(var / n) if var > 0 else 0.0


def stats(trades: list[Trade], start: datetime | None = None, end: datetime | None = None) -> dict:
    """Plain-language numbers for a list of trades. R is profit in units of the amount risked."""
    n = len(trades)
    if not n:
        return {"trades": 0, "win_rate_pct": 0.0, "avg_r": 0.0, "avg_pips": 0.0, "total_r": 0.0,
                "profit_factor": 0.0, "t_stat": 0.0, "max_drawdown_r": 0.0, "avg_spread_pips": 0.0}
    rs = [t.r for t in trades]
    wins = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    # Trades that happen together aren't independent, so the t-stat is computed on monthly totals,
    # counting months with no trades as zero.
    start = start or min(t.entry_time for t in trades)
    end = end or max(t.entry_time for t in trades) + HOUR
    monthly = dict.fromkeys(_months_between(start, end), 0.0)
    for t in trades:
        if _month(t.entry_time) in monthly:
            monthly[_month(t.entry_time)] += t.r
    peak = equity = drawdown = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": n,
        "win_rate_pct": round(100 * sum(r > 0 for r in rs) / n, 1),
        "avg_r": round(sum(rs) / n, 3),
        "avg_pips": round(sum(t.pips for t in trades) / n, 2),
        "total_r": round(sum(rs), 1),
        "profit_factor": round(wins / losses, 2) if losses else float("inf"),
        "t_stat": round(t_stat(list(monthly.values())), 2),
        "max_drawdown_r": round(drawdown, 1),
        "avg_spread_pips": round(sum(t.spread_pips for t in trades) / n, 2),
    }


# -- walk-forward ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """What a strategy must show in its training years before the walk-forward will trade it."""

    min_trades: int = 60
    min_avg_r: float = 0.0
    min_t_stat: float = 2.0


@dataclass
class Window:
    train_start: datetime
    test_start: datetime
    test_end: datetime
    chosen: list[dict] = field(default_factory=list)
    test: dict = field(default_factory=dict)


def _in(trades, start, end):
    return [t for t in trades if start <= t.entry_time < end]


def choose(by_strategy: dict[str, list[Trade]], start: datetime, end: datetime, rule: Selection) -> list[dict]:
    chosen = []
    for strat in STRATEGIES:
        s = stats(_in(by_strategy.get(strat.key, []), start, end), start, end)
        if s["trades"] >= rule.min_trades and s["avg_r"] > rule.min_avg_r and s["t_stat"] >= rule.min_t_stat:
            chosen.append({"key": strat.key, "signal": strat.signal, "inverted": strat.inverted, "exit": strat.exit,
                           "label": strat.label, **s})
    return chosen


def walk_forward(trades: list[Trade], first_test: datetime, holdout_start: datetime, train_years: int = 3,
                 test_months: int = 6, rule: Selection = Selection()) -> tuple[list[Window], list[Trade]]:
    by_strategy = defaultdict(list)
    for t in trades:
        by_strategy[t.strategy].append(t)
    windows, oos = [], []
    start = first_test
    while start < holdout_start:
        end = min(_add_months(start, test_months), holdout_start)
        train_start = _add_months(start, -12 * train_years)
        w = Window(train_start, start, end, choose(by_strategy, train_start, start, rule))
        picked = [t for c in w.chosen for t in _in(by_strategy[c["key"]], start, end)]
        # A trade must also finish before the holdout starts, so the holdout stays untouched.
        picked = [t for t in picked if t.exit_time <= holdout_start]
        w.test = stats(picked, start, end)
        windows.append(w)
        oos.extend(picked)
        start = end
    return windows, sorted(oos, key=lambda t: t.entry_time)


def _add_months(t: datetime, months: int) -> datetime:
    y, m = divmod(t.month - 1 + months, 12)
    return t.replace(year=t.year + y, month=m + 1)


# -- the desk with 100 USD -----------------------------------------------------------------------------------


def desk_simulation(trades: list[Trade], budget: float = 100.0, risk_pct: float = 0.5, max_open: int = 3,
                    max_per_day: int = 3, api_cost_per_trade: float = 0.15) -> dict:
    """Run trades through the desk's limits on a 100 USD budget: one per pair, 3 open, 3 new per day.

    Every trade costs one debate's worth of Claude API spend, because the live desk debates every trade.
    """
    equity = peak = budget
    worst = 0.0
    halved = None  # when half the starting budget was first gone
    taken = []
    open_until: dict[str, datetime] = {}
    per_day = defaultdict(int)
    for t in sorted(trades, key=lambda t: t.entry_time):
        open_until = {p: until for p, until in open_until.items() if until > t.entry_time}
        day = t.entry_time.date()
        if t.pair in open_until or len(open_until) >= max_open or per_day[day] >= max_per_day:
            continue
        open_until[t.pair] = t.exit_time
        per_day[day] += 1
        equity += t.r * equity * risk_pct / 100
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
        taken.append(t)
        if halved is None and equity <= budget / 2:
            halved = {"after_trades": len(taken), "date": t.exit_time.date().isoformat()}
        if equity <= 0:
            break
    api = api_cost_per_trade * len(taken)
    return {
        "trades": len(taken),
        "end_equity_usd": round(equity, 2),
        "trading_result_usd": round(equity - budget, 2),
        "api_cost_usd": round(api, 2),
        "net_after_api_usd": round(equity - budget - api, 2),
        "max_drawdown_pct": round(100 * worst, 1),
        "half_budget_gone": halved,
    }


# -- the pass bar ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PassBar:
    min_trades: int = 300
    min_avg_r: float = 0.05
    min_t_stat: float = 2.0
    min_profit_factor: float = 1.1
    max_drawdown_pct: float = 20.0
    min_positive_windows_pct: float = 60.0
    stress_extra_pips: float = 0.5  # still profitable when every trade costs this much more
    holdout_min_avg_r: float = 0.0
    pays_for_debates: bool = True  # profit per trade at normal size covers one debate's API cost


def check_pass_bar(bar: PassBar, oos: dict, stressed: dict, windows: list[Window], desk: dict, holdout: dict,
                   risk_usd: float, api_cost: float) -> list[dict]:
    tested = [w for w in windows if w.test["trades"]]
    positive = 100 * sum(w.test["avg_r"] > 0 for w in tested) / len(tested) if tested else 0.0
    per_trade_usd = oos["avg_r"] * risk_usd
    checks = [
        ("Enough out-of-sample trades", f"at least {bar.min_trades}", oos["trades"], oos["trades"] >= bar.min_trades),
        ("Average profit per trade after costs", f"at least {bar.min_avg_r:+.2f}R", f"{oos['avg_r']:+.3f}R",
         oos["avg_r"] >= bar.min_avg_r),
        ("Not luck (t-stat on monthly results)", f"at least {bar.min_t_stat}", oos["t_stat"],
         oos["t_stat"] >= bar.min_t_stat),
        ("Profit factor (gains / losses)", f"at least {bar.min_profit_factor}", oos["profit_factor"],
         oos["profit_factor"] >= bar.min_profit_factor),
        ("Test windows that made money", f"at least {bar.min_positive_windows_pct:.0f}%", f"{positive:.0f}%",
         positive >= bar.min_positive_windows_pct),
        ("Survives higher costs", f"still positive with +{bar.stress_extra_pips} pips per trade",
         f"{stressed['avg_r']:+.3f}R", stressed["avg_r"] > 0),
        ("Worst drop on the 100 USD budget", f"at most {bar.max_drawdown_pct:.0f}%", f"{desk['max_drawdown_pct']}%",
         desk["max_drawdown_pct"] <= bar.max_drawdown_pct and desk["trades"] > 0),
        ("Holdout year (never used to choose anything)", f"average above {bar.holdout_min_avg_r:+.2f}R",
         f"{holdout['avg_r']:+.3f}R over {holdout['trades']} trades",
         holdout["trades"] > 0 and holdout["avg_r"] > bar.holdout_min_avg_r),
    ]
    if bar.pays_for_debates:
        checks.append(("Pays for its own debate", f"profit per trade above {api_cost:.2f} USD API cost",
                       f"{per_trade_usd:+.3f} USD", per_trade_usd > api_cost))
    return [{"check": c, "needs": n, "got": g, "passed": bool(p)} for c, n, g, p in checks]


# -- the whole run -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    pairs: list[str]
    first_test: datetime = datetime(2019, 1, 1, tzinfo=timezone.utc)
    holdout_start: datetime = datetime(2025, 10, 22, tzinfo=timezone.utc)
    end: datetime = datetime(2026, 10, 22, tzinfo=timezone.utc)
    train_years: int = 3
    test_months: int = 6
    costs: Costs = Costs()
    selection: Selection = Selection()
    bar: PassBar = PassBar()
    budget_usd: float = 100.0
    risk_pct: float = 0.5
    api_cost_per_trade: float = 0.15


def run(plan: Plan, folder: Path = history.HISTORY_DIR, out: Path = OUT_DIR, workers: int = 4) -> dict:
    trades = all_trades(plan.pairs, plan.costs, folder=folder, cache=out, workers=workers)
    by_strategy = defaultdict(list)
    for t in trades:
        by_strategy[t.strategy].append(t)
    first_trade = trades[0].entry_time if trades else plan.first_test

    # 1. Every strategy over the whole history before the holdout (fixed rules, nothing fitted).
    full = {s.key: {"label": s.label, **stats(_in(by_strategy[s.key], first_trade, plan.holdout_start),
                                                first_trade, plan.holdout_start)} for s in STRATEGIES}

    # 2. Walk-forward: choose on the past 3 years, trade the next 6 months.
    windows, oos = walk_forward(trades, plan.first_test, plan.holdout_start, plan.train_years, plan.test_months,
                                plan.selection)
    oos_stats = stats(oos, plan.first_test, plan.holdout_start)
    stressed = stats([_costlier(t, plan.bar.stress_extra_pips) for t in oos], plan.first_test, plan.holdout_start)
    desk = desk_simulation(oos, plan.budget_usd, plan.risk_pct, api_cost_per_trade=plan.api_cost_per_trade)

    # 3. The holdout year, with strategies chosen on the 3 years before it.
    hold_train = _add_months(plan.holdout_start, -12 * plan.train_years)
    hold_chosen = choose(by_strategy, hold_train, plan.holdout_start, plan.selection)
    hold_trades = sorted((t for c in hold_chosen for t in _in(by_strategy[c["key"]], plan.holdout_start, plan.end)),
                         key=lambda t: t.entry_time)
    holdout = stats(hold_trades, plan.holdout_start, plan.end)
    holdout_desk = desk_simulation(hold_trades, plan.budget_usd, plan.risk_pct,
                                   api_cost_per_trade=plan.api_cost_per_trade)

    risk_usd = plan.budget_usd * plan.risk_pct / 100
    checks = check_pass_bar(plan.bar, oos_stats, stressed, windows, desk, holdout, risk_usd, plan.api_cost_per_trade)
    screen = _screen_baseline(by_strategy, first_trade, plan.end)
    for side in ("natural", "opposite"):
        key = "screen_all/intraday" if side == "natural" else "screen_all_inverted/intraday"
        screen[side]["desk"] = desk_simulation(_in(by_strategy[key], first_trade, plan.end), plan.budget_usd,
                                               plan.risk_pct, api_cost_per_trade=plan.api_cost_per_trade)

    # What the quant seat may use live: chosen on the latest 3 years, which run up to the end of the data.
    live_train = _add_months(plan.end, -12 * plan.train_years)
    live = choose(by_strategy, live_train, plan.end, plan.selection)
    for c in live:
        c.update(stop_atr=EXITS_BY_NAME[c["exit"]].stop_atr, max_hours=EXITS_BY_NAME[c["exit"]].max_hours)
    passed = all(c["passed"] for c in checks)
    result = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "plan": {"pairs": plan.pairs, "first_test": plan.first_test.date().isoformat(),
                 "holdout_start": plan.holdout_start.date().isoformat(), "end": plan.end.date().isoformat(),
                 "first_trade": first_trade.date().isoformat(), "costs": asdict(plan.costs),
                 "selection": asdict(plan.selection), "pass_bar": asdict(plan.bar), "risk_pct": plan.risk_pct,
                 "budget_usd": plan.budget_usd, "api_cost_per_trade": plan.api_cost_per_trade},
        "strategies_full_history": full,
        "screen_baseline": screen,
        "walk_forward": [{"train_start": w.train_start.date().isoformat(), "test_start": w.test_start.date().isoformat(),
                          "test_end": w.test_end.date().isoformat(), "chosen": [c["key"] for c in w.chosen],
                          "test": w.test} for w in windows],
        "out_of_sample": oos_stats,
        "out_of_sample_by_pair": {p: stats([t for t in oos if t.pair == p], plan.first_test, plan.holdout_start)
                                  for p in plan.pairs},
        "stressed": stressed,
        "desk_simulation": desk,
        "holdout": {"chosen": [c["key"] for c in hold_chosen], **holdout, "desk": holdout_desk},
        "pass_bar": checks,
        "passed": passed,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(result, indent=1, default=str))
    # The quant seat only gets strategies to lean on if the whole system passed the bar.
    validation = {"generated": result["generated"], "passed": passed, "validated": live if passed else [],
                  "candidates_if_bar_passed": live}
    (out / "quant_validation.json").write_text(json.dumps(validation, indent=1, default=str))
    return result


def _costlier(t: Trade, extra_pips: float) -> Trade:
    pips = t.pips - extra_pips
    return Trade(**{**asdict(t), "pips": pips, "r": pips / t.stop_pips})


def _screen_baseline(by_strategy, start, end) -> dict:
    """The desk's code screen today: its setups traded each way, and what a coin flip between them costs."""
    natural = stats(_in(by_strategy["screen_all/intraday"], start, end), start, end)
    opposite = stats(_in(by_strategy["screen_all_inverted/intraday"], start, end), start, end)
    return {"natural": natural, "opposite": opposite,
            "coin_flip_avg_r": round((natural["avg_r"] + opposite["avg_r"]) / 2, 3)}



def plan_from_settings(settings) -> Plan:
    raw = settings.backtest

    def day(key, default):
        value = raw.get(key)
        if value is None:
            return default
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)

    defaults = Plan(pairs=settings.pairs)
    return Plan(
        pairs=settings.pairs,
        first_test=day("first_test", defaults.first_test),
        holdout_start=day("holdout_start", defaults.holdout_start),
        end=day("end", defaults.end),
        train_years=int(raw.get("train_years", defaults.train_years)),
        test_months=int(raw.get("test_months", defaults.test_months)),
        costs=Costs(**{"max_spread_pips": settings.risk.max_spread_pips,
                       "min_stop_pips": settings.risk.min_stop_pips + 1, **(raw.get("costs") or {})}),
        selection=Selection(**(raw.get("selection") or {})),
        bar=PassBar(**(raw.get("pass_bar") or {})),
        budget_usd=settings.starting_capital,
        risk_pct=settings.risk.risk_per_trade_pct,
        api_cost_per_trade=float(raw.get("api_cost_per_trade", defaults.api_cost_per_trade)),
    )


# -- the report --------------------------------------------------------------------------------------------


def _row(name: str, s: dict) -> str:
    return (f"| {name} | {s['trades']:,} | {s['win_rate_pct']}% | {s['avg_r']:+.3f}R | {s['avg_pips']:+.1f} | "
            f"{s['profit_factor']} | {s['t_stat']} |")


HEADER = "| Strategy | Trades | Won | Average per trade | Average pips | Profit factor | t-stat |\n|---|---|---|---|---|---|---|"


def render_markdown(r: dict) -> str:
    plan = r["plan"]
    verdict = "PASSED" if r["passed"] else "NOT PASSED"
    lines = [
        "# Backtest results",
        "",
        f"Generated {r['generated']} by `tradeboy backtest`. Pairs: {', '.join(plan['pairs'])}. "
        f"Hourly OANDA candles from {plan['first_trade']} to {plan['end']}, with real bid and ask prices.",
        "",
        f"## Verdict: {verdict}",
        "",
        "Every line of the pass bar must hold before demo trading starts.",
        "",
        "| Check | Needs | Got | |",
        "|---|---|---|---|",
    ]
    for c in r["pass_bar"]:
        lines.append(f"| {c['check']} | {c['needs']} | {c['got']} | {'pass' if c['passed'] else 'FAIL'} |")
    chosen_any = any(w["chosen"] for w in r["walk_forward"])
    lines += [
        "",
        "## How to read this",
        "",
        "- **R** is profit measured in units of the amount risked. +0.10R means that, on average, each trade "
        "made a tenth of what it risked. At the desk's 0.5% risk on 100 USD, 1R is 0.50 USD.",
        "- **t-stat** says how sure we can be that the average is really above zero and not luck. About 2 or "
        "more is the usual bar. It is computed on monthly totals, because trades that happen together are not "
        "independent.",
        "- **Costs** are all in: the real historical spread paid on the way in and out, "
        f"{plan['costs']['slippage_pips']} pips of slippage each way, and overnight financing (the interest "
        f"rate gap, minus OANDA's {plan['costs']['financing_markup_pct']}% a year charge).",
        "- A stop and a target touched in the same hour count as a stop (the worse case).",
        "",
        "## 1. The desk's code screen, as it works today",
        "",
        "The screen flags setups for a debate. Traded in the direction each setup implies (and, to check, in "
        "the opposite direction), with the desk's fallback stop of 1.5x the hourly ATR, a 1.5R target and a "
        "24-hour limit:",
        "",
        HEADER,
        _row("Code screen, natural direction", r["screen_baseline"]["natural"]),
        _row("Code screen, opposite direction", r["screen_baseline"]["opposite"]),
        "",
        f"A coin flip between the two averages {r['screen_baseline']['coin_flip_avg_r']:+.3f}R per trade: that "
        "is roughly what costs take from every trade at this stop size.",
        "",
    ]
    for side in ("natural", "opposite"):
        d = r["screen_baseline"][side]["desk"]
        half = d["half_budget_gone"]
        lines.append(f"- 100 USD run through the desk's limits ({side} direction, at most 3 trades a day, 0.5% risk "
                     f"each): {d['trades']:,} trades, ends at {d['end_equity_usd']} USD"
                     + (f"; half the budget was gone after {half['after_trades']} trades ({half['date']})" if half else "")
                     + f". Debating every one would have cost another {d['api_cost_usd']:,.0f} USD of Claude credits.")
    lines += [
        "",
        "## 2. Every strategy over the whole history (before the holdout year)",
        "",
        "Fixed rules with nothing fitted. 'OPPOSITE of' rows trade against the signal. Before choosing, these "
        "are the raw numbers; a single good row here can still be luck, which is what step 3 is for.",
        "",
        HEADER,
    ]
    for s in r["strategies_full_history"].values():
        lines.append(_row(s["label"], s))
    lines += [
        "",
        "## 3. Walk-forward (the honest test)",
        "",
        f"Each 6-month window trades only the strategies that, over the 3 years before it, had at least "
        f"{plan['selection']['min_trades']} trades, a positive average and a t-stat of at least "
        f"{plan['selection']['min_t_stat']}. It is then judged on the 6 months it was not chosen on.",
        "",
        "| Test window | Chosen strategies | Trades | Average per trade |",
        "|---|---|---|---|",
    ]
    for w in r["walk_forward"]:
        lines.append(f"| {w['test_start']} to {w['test_end']} | {', '.join(w['chosen']) or 'none qualified'} | "
                     f"{w['test']['trades']} | {w['test']['avg_r']:+.3f}R |")
    if not chosen_any:
        lines += ["", "No strategy qualified in any window, so there were no out-of-sample trades to judge."]
    else:
        lines += ["", HEADER, _row("All out-of-sample trades", r["out_of_sample"]),
                  _row(f"Same, {plan['pass_bar']['stress_extra_pips']} pips costlier", r["stressed"])]
    h = r["holdout"]
    lines += [
        "",
        "## 4. Holdout year",
        "",
        f"From {plan['holdout_start']} to {plan['end']}, with strategies chosen on the 3 years before: "
        f"{', '.join(h['chosen']) or 'none qualified'}. {h['trades']} trades, {h['avg_r']:+.3f}R on average.",
        "",
        "## What the quant seat does with this",
        "",
        "The quant seat only leans on strategies that pass this whole process. Until the pass bar is met it "
        "tells the debate, in plain words, that it has no tested edge, and its side carries no weight.",
        "",
    ]
    return "\n".join(lines)
