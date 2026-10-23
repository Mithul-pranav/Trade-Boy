"""The research desk: runs the pre-registered hypotheses in research/hypotheses.yaml and judges them.

Used by the quant researcher (Turtle rules) and the ML researcher (the logistic model). Every study is
judged the same way, against the desk's unchanged pass bar:
- Only registered variants run, and every one counts as a trial (plus the trials before the register),
  with a Holm correction so luck across many ideas isn't mistaken for skill.
- Walk-forward: the verdict comes from test windows the variant was not chosen on (or, for the model,
  from years it was not fitted on). The last year is a holdout, checked once.
- A strategy is promoted to the live quant seat only if it passes every check. Otherwise the report
  says plainly that it failed.

Needs the research extra (numpy, pandas) and history in data/research (see `download`).
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import history
from .backtest import PassBar, Selection, Trade, Window, _add_months, _in, check_pass_bar, desk_simulation, stats
from .config import ROOT

REGISTRY = ROOT / "research" / "hypotheses.yaml"
DATA_DIR = ROOT / "data" / "research"
PROMOTED = DATA_DIR / "promoted.json"
START = datetime(2004, 1, 1, tzinfo=timezone.utc)
FIRST_TEST = datetime(2008, 1, 1, tzinfo=timezone.utc)


def load_registry(path: Path = REGISTRY) -> dict:
    return yaml.safe_load(path.read_text())


def total_trials(registry: dict) -> int:
    prior = sum((registry.get("prior_trials") or {}).values())
    return prior + sum(len(h["variants"]) for h in registry["hypotheses"])


def p_value(t: float) -> float:
    """One-sided: the chance of a t-stat this high if the true average were zero (normal approximation)."""
    return 0.5 * math.erfc(t / math.sqrt(2))


def holm(pvalues: dict[str, float], extra_trials: int = 0) -> dict[str, float]:
    """Holm-adjusted p-values. extra_trials are earlier tests whose p-values aren't known: they count as
    trials that failed (p = 1), which makes the correction stricter, never looser."""
    m = len(pvalues) + extra_trials
    out, running = {}, 0.0
    for rank, (key, p) in enumerate(sorted(pvalues.items(), key=lambda kv: kv[1])):
        running = max(running, min(1.0, (m - rank) * p))
        out[key] = round(running, 4)
    return out


def _units(t: Trade) -> int:
    m = re.search(r"\((\d+) units\)", t.how)
    return int(m.group(1)) if m else 1


def _stressed(t: Trade, extra_pips: float) -> Trade:
    pips = t.pips - extra_pips * _units(t)
    return Trade(**{**asdict(t), "pips": pips, "r": pips / t.stop_pips})


def evaluate(by_variant: dict[str, list[Trade]], plan: dict, select: bool = True, extra_trials: int = 0) -> dict:
    """The pass-bar verdict for one hypothesis.

    select=True: walk-forward choice between variants (each 6-month window trades only the variants
    that showed an edge in the years before). select=False: every trade is already out of sample
    (a model refitted on earlier years only), so all of them count.
    """
    first_test, holdout, end = plan["first_test"], plan["holdout_start"], plan["end"]
    rule, bar = plan["selection"], plan["bar"]
    first_trade = min((t.entry_time for ts in by_variant.values() for t in ts), default=first_test)
    full = {k: stats(_in(ts, first_trade, holdout), first_trade, holdout) for k, ts in by_variant.items()}
    adjusted = holm({k: p_value(s["t_stat"]) for k, s in full.items()}, extra_trials)
    for k in full:
        full[k]["holm_p"] = adjusted[k]

    def chosen_between(start, stop):
        if not select:
            return list(by_variant)
        picked = []
        for k, ts in by_variant.items():
            s = stats(_in(ts, start, stop), start, stop)
            if s["trades"] >= rule.min_trades and s["avg_r"] > rule.min_avg_r and s["t_stat"] >= rule.min_t_stat:
                picked.append(k)
        return picked

    windows, oos = [], []
    start = first_test
    while start < holdout:
        stop = min(_add_months(start, plan["test_months"]), holdout)
        train = _add_months(start, -12 * plan["train_years"])
        keys = chosen_between(train, start)
        picked = [t for k in keys for t in _in(by_variant[k], start, stop) if t.exit_time <= holdout]
        w = Window(train, start, stop, [{"key": k} for k in keys], stats(picked, start, stop))
        windows.append(w)
        oos.extend(picked)
        start = stop
    oos.sort(key=lambda t: t.entry_time)
    oos_stats = stats(oos, first_test, holdout)
    stressed = stats([_stressed(t, bar.stress_extra_pips) for t in oos], first_test, holdout)
    desk = desk_simulation(oos, plan["budget_usd"], plan["risk_pct"], api_cost_per_trade=plan["api_cost"])
    hold_keys = chosen_between(_add_months(holdout, -12 * plan["train_years"]), holdout)
    hold_trades = sorted((t for k in hold_keys for t in _in(by_variant[k], holdout, end)), key=lambda t: t.entry_time)
    hold = stats(hold_trades, holdout, end)
    risk_usd = plan["budget_usd"] * plan["risk_pct"] / 100
    checks = check_pass_bar(bar, oos_stats, stressed, windows, desk, hold, risk_usd, plan["api_cost"])
    # A strategy family also has to clear the multiple-testing correction on its full history.
    best = min(full.items(), key=lambda kv: kv[1]["holm_p"]) if full else ("none", {"holm_p": 1.0})
    checks.append({"check": "Not luck after counting every idea tried (Holm-adjusted p)",
                   "needs": "at most 0.05", "got": f"{best[1]['holm_p']} ({best[0]})",
                   "passed": best[1]["holm_p"] <= 0.05})
    live_keys = chosen_between(_add_months(end, -12 * plan["train_years"]), end)
    return {
        "variants_full_history": full,
        "walk_forward": [{"test_start": w.test_start.date().isoformat(), "test_end": w.test_end.date().isoformat(),
                          "chosen": [c["key"] for c in w.chosen], "test": w.test} for w in windows],
        "out_of_sample": oos_stats,
        "out_of_sample_by_pair": {p: stats([t for t in oos if t.pair == p], first_test, holdout)
                                  for p in sorted({t.pair for t in oos})},
        "stressed": stressed,
        "desk_simulation": desk,
        "holdout": {"chosen": hold_keys, **hold},
        "pass_bar": checks,
        "passed": all(c["passed"] for c in checks),
        "live_candidates": live_keys,
    }


def plan_from_settings(settings) -> dict:
    bt = settings.backtest or {}
    bar = PassBar(**(bt.get("pass_bar") or {}))
    sel = bt.get("selection") or {}
    day = lambda key, default: datetime.fromisoformat(str(bt.get(key, default))).replace(tzinfo=timezone.utc)  # noqa: E731
    costs = bt.get("costs") or {}
    return {
        "pairs": list(settings.pairs),
        "first_test": FIRST_TEST,
        "holdout_start": day("holdout_start", "2025-10-22"),
        "end": day("end", "2026-10-22"),
        "train_years": int(bt.get("train_years", 3)),
        "test_months": int(bt.get("test_months", 6)),
        "selection": Selection(**sel),
        "bar": bar,
        "budget_usd": float(settings.starting_capital),
        "risk_pct": float(settings.risk.risk_per_trade_pct),
        "api_cost": float(bt.get("api_cost_per_trade", 0.15)),
        "slippage_pips": float(costs.get("slippage_pips", 0.2)),
        "markup_pct": float(costs.get("financing_markup_pct", 0.8)),
    }


# -- data --------------------------------------------------------------------------------------------------


def download(client, pairs: list[str], folder: Path = DATA_DIR) -> None:
    """Daily and 4-hour candles with bid and ask since 2004, and short-term interest rates. Tops up."""
    for pair in pairs:
        for granularity in ("D", "H4"):
            history.download(client, pair, granularity, START, folder=folder)
    history.download_rates(folder=folder)


def _load(pairs: list[str], folder: Path) -> dict[str, tuple]:
    return {p: (history.load(p, "D", folder), history.load(p, "H4", folder)) for p in pairs}


def _cut(data: dict[str, tuple], end: datetime) -> dict[str, tuple]:
    return {p: ([b for b in d if b.time < end], [b for b in h if b.time < end]) for p, (d, h) in data.items()}


# -- the studies -------------------------------------------------------------------------------------------


def turtle_study(data: dict[str, tuple], rates, plan: dict, variants: list[str], extra_trials: int) -> dict:
    from . import turtle

    by_variant = defaultdict(list)
    for name in variants:
        v = turtle.VARIANTS_BY_NAME[name]
        for pair, (daily, h4) in data.items():
            trades, _ = turtle.simulate(pair, daily, v, rates, plan["slippage_pips"], plan["markup_pct"], intraday=h4)
            by_variant[name].extend(trades)
        by_variant[name].sort(key=lambda t: t.entry_time)
    return evaluate(dict(by_variant), plan, select=True, extra_trials=extra_trials)


def ml_study(data: dict[str, tuple], rates, plan: dict, extra_trials: int, shuffles: int = 20) -> dict:
    from . import ml

    prepared = ml.panel(data, rates)
    first, last = plan["first_test"].year, plan["end"].year
    models = ml.walk_forward_models(prepared, first, last)
    real = ml.trades(prepared, models, plan["slippage_pips"], plan["markup_pct"])
    result = evaluate({"ml_logit": real}, plan, select=False, extra_trials=extra_trials)
    # The control: the same model fitted on shuffled labels. Real skill should beat nearly all of them.
    noise = []
    for seed in range(shuffles):
        fake = ml.trades(prepared, ml.walk_forward_models(prepared, first, last, shuffle_seed=seed),
                         plan["slippage_pips"], plan["markup_pct"])
        noise.append(stats(_in(fake, plan["first_test"], plan["holdout_start"]))["avg_r"])
    beaten = sum(result["out_of_sample"]["avg_r"] > n for n in noise)
    result["shuffled_label_control"] = {"runs": shuffles, "avg_r_each": noise, "beaten": beaten}
    result["pass_bar"].append({"check": "Beats the same model trained on shuffled labels",
                               "needs": f"better than at least {shuffles - 1} of {shuffles}",
                               "got": f"better than {beaten}", "passed": beaten >= shuffles - 1})
    result["passed"] = all(c["passed"] for c in result["pass_bar"])
    # The model the live seat would use: fitted on everything up to the end of the data.
    result["live_model"] = ml.walk_forward_models(prepared, last + 1, last + 1).get(last + 1)
    return result


def run(settings, folder: Path = DATA_DIR, registry_path: Path = REGISTRY, shuffles: int = 20) -> dict:
    """Run every registered hypothesis, write the results and the promotions for the quant seat."""
    registry = load_registry(registry_path)
    plan = plan_from_settings(settings)
    trials = total_trials(registry)
    data = _cut(_load(plan["pairs"], folder), plan["end"])
    rates = history.ShortRates.load(folder)
    studies = {}
    for h in registry["hypotheses"]:
        if h.get("status") == "retired":
            continue
        others = trials - len(h["variants"])  # every other idea ever tried counts in the correction
        if h["id"].startswith("H1-turtle"):
            studies[h["id"]] = turtle_study(data, rates, plan, h["variants"], others)
        elif h["id"].startswith("H2-ml"):
            studies[h["id"]] = ml_study(data, rates, plan, others, shuffles)
        else:
            continue
        studies[h["id"]]["hypothesis"] = {k: h[k] for k in ("id", "owner", "registered", "idea")}
    promoted = []
    for hid, r in studies.items():
        if not r["passed"]:
            continue
        o = r["out_of_sample"]
        base = {"hypothesis": hid, "trades": o["trades"], "avg_r": o["avg_r"], "t_stat": o["t_stat"],
                "win_rate_pct": o["win_rate_pct"]}
        if hid.startswith("H1-turtle"):
            promoted += [{**base, "kind": "turtle", "variant": k} for k in r["live_candidates"]]
        elif r.get("live_model"):
            promoted.append({**base, "kind": "ml_logit", "model": r["live_model"]})
    result = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "total_trials_counted": trials,
        "plan": {k: (v.isoformat() if isinstance(v, datetime) else asdict(v) if hasattr(v, "__dataclass_fields__")
                     else v) for k, v in plan.items()},
        "studies": studies,
        "promoted": promoted,
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results.json").write_text(json.dumps(result, indent=1, default=str))
    (folder / "promoted.json").write_text(json.dumps({"generated": result["generated"], "promoted": promoted},
                                                     indent=1, default=str))
    return result


def load_promoted(path: Path = PROMOTED) -> list[dict]:
    """Strategies that passed the bar, for the live quant seat. Empty until something passes."""
    return json.loads(path.read_text()).get("promoted", []) if path.exists() else []
