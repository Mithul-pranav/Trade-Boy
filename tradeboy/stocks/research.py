"""The quant researcher: does the price keep drifting after a big earnings reaction? Code only.

Post-earnings drift is one of the best documented effects in stock research, and it can be tested
honestly because it needs no reading: the signal is the stock's move against QQQ on the day the market
first reacts to the report. Claude's memory of old news plays no part, so the past is a fair test.

The test, as pre-registered here before any result was seen:
- Earnings dates: SEC 8-K filings with item 2.02, timestamped to the second. A report filed after
  16:00 New York time is first traded the next session; one filed before 9:30, the same session.
- Signal: the reaction day's return minus QQQ's. Beyond a threshold, trade in the same direction.
- Entry at the next session's open (the signal uses the reaction day's close, so no peeking).
- Stop 1.5x the 14-day ATR measured before the report; a gap through it fills at the open. Exit at the
  close after the holding period. R is the stop distance.
- Costs: cost_bps_per_side on entry and exit, plus the borrow fee on shorts.
- Variants: thresholds 3% and 5%, holds of 5, 10 and 20 sessions: 6 trials, Holm-corrected.
- Walk-forward: each 6-month window trades only variants that showed an edge in the 3 years before it.
  The last year is a holdout, checked once. The pass bar is in config/stocks.yaml.

A known bias, stated up front: today's 20 largest tech companies are a list chosen with hindsight. They
are the winners, so long trades will look better than they would have at the time. The report shows
long and short results separately for that reason.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from ..config import ROOT
from .market import NEW_YORK
from .models import Bar

DATA_DIR = ROOT / "data" / "stocks_research"
PROMOTED = DATA_DIR / "promoted.json"
THRESHOLDS = (3.0, 5.0)
HOLDS = (5, 10, 20)
STOP_ATR = 1.5


@dataclass(frozen=True)
class Trade:
    symbol: str
    variant: str
    side: int  # +1 long, -1 short
    report: date
    entry_day: date
    exit_day: date
    entry: float
    exit: float
    stop: float
    r: float  # after costs
    how: str


# -- data ----------------------------------------------------------------------------------------

def save_bars(path: Path, bars: list[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.time.astimezone(NEW_YORK).date().isoformat(), b.open, b.high, b.low, b.close, b.volume])


def load_bars(path: Path) -> list[Bar]:
    with path.open() as f:
        return [Bar(datetime.combine(date.fromisoformat(r["date"]), time(16), NEW_YORK), float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"]), float(r["volume"])) for r in csv.DictReader(f)]


def earnings_8k_times(edgar, symbol: str) -> list[datetime]:
    """Acceptance times of every 8-K with item 2.02, from all of the company's EDGAR submission files."""
    from .sec import SUBMISSIONS_URL

    cik = edgar.cik(symbol)
    main = edgar._get(SUBMISSIONS_URL.format(cik=cik)).json()
    tables = [main["filings"]["recent"]]
    for extra in main["filings"].get("files", []):
        tables.append(edgar._get(f"https://data.sec.gov/submissions/{extra['name']}").json())
    out = []
    for t in tables:
        for form, items, accepted in zip(t["form"], t.get("items", []), t.get("acceptanceDateTime", [])):
            if form == "8-K" and "2.02" in (items or "") and accepted:
                out.append(datetime.fromisoformat(accepted.replace("Z", "+00:00")))
    return sorted(set(out))


def download(settings, yahoo, edgar, start: date) -> None:
    for symbol in settings.symbols + [settings.benchmark]:
        save_bars(DATA_DIR / f"{symbol}.csv", yahoo.daily_bars(symbol, start))
    reports = {s: [t.isoformat() for t in earnings_8k_times(edgar, s)] for s in settings.symbols}
    (DATA_DIR / "earnings.json").write_text(json.dumps(reports, indent=1))


# -- the test ------------------------------------------------------------------------------------

def reaction_day(accepted: datetime, days: list[date]) -> int | None:
    """Index of the first session that could trade on a report accepted at this time."""
    ny = accepted.astimezone(NEW_YORK)
    first = ny.date() if ny.time() < time(16) else ny.date() + timedelta(days=1)
    for i, d in enumerate(days):
        if d >= first:
            return i
    return None


def _atr(bars: list[Bar], end: int, period: int = 14) -> float:
    ranges = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
              for a, b in zip(bars[end - period - 1:end - 1], bars[end - period:end])]
    return sum(ranges) / len(ranges) if ranges else 0.0


def simulate(symbol: str, bars: list[Bar], bench: list[Bar], reports: list[datetime], costs: dict) -> list[Trade]:
    days = [b.time.astimezone(NEW_YORK).date() for b in bars]
    bench_close = {b.time.astimezone(NEW_YORK).date(): b.close for b in bench}
    side_cost = costs.get("cost_bps_per_side", 5) / 10_000
    borrow_day = costs.get("borrow_bps_per_year", 30) / 10_000 / 252
    trades = []
    for accepted in reports:
        i = reaction_day(accepted, days)
        if i is None or i < 20 or i + 1 >= len(bars):
            continue
        d0, d1 = days[i - 1], days[i]
        if d0 not in bench_close or d1 not in bench_close:
            continue
        excess = (bars[i].close / bars[i - 1].close - bench_close[d1] / bench_close[d0]) * 100
        atr = _atr(bars, i)  # measured before the reaction day
        if atr <= 0:
            continue
        for threshold in THRESHOLDS:
            if abs(excess) < threshold:
                continue
            side = 1 if excess > 0 else -1
            for hold in HOLDS:
                if i + hold >= len(bars):
                    continue
                entry = bars[i + 1].open
                stop = entry - side * STOP_ATR * atr
                exit_price, exit_i, how = bars[i + hold].close, i + hold, f"held {hold} sessions"
                for j in range(i + 1, i + hold + 1):
                    b = bars[j]
                    if j > i + 1 and (b.open - stop) * side <= 0:
                        exit_price, exit_i, how = b.open, j, "gapped through the stop"
                        break
                    if (b.low - stop if side > 0 else stop - b.high) <= 0:
                        exit_price, exit_i, how = stop, j, "stopped"
                        break
                risk = abs(entry - stop)
                gross = (exit_price - entry) * side
                cost = entry * side_cost + exit_price * side_cost + (entry * borrow_day * (exit_i - i) if side < 0 else 0)
                trades.append(Trade(symbol, f"t{threshold:g}_h{hold}", side, d1, days[i + 1], days[exit_i], round(entry, 4),
                                    round(exit_price, 4), round(stop, 4), (gross - cost) / risk, how))
    return trades


def stats(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0, "avg_r": 0.0, "t_stat": 0.0, "profit_factor": 0.0, "win_pct": 0.0}
    rs = [t.r for t in trades]
    months: dict[str, float] = {}
    for t in trades:
        months[t.entry_day.strftime("%Y-%m")] = months.get(t.entry_day.strftime("%Y-%m"), 0.0) + t.r
    m = list(months.values())
    mean_m = sum(m) / len(m)
    sd_m = math.sqrt(sum((x - mean_m) ** 2 for x in m) / (len(m) - 1)) if len(m) > 1 else 0.0
    won, lost = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    return {
        "trades": len(rs),
        "avg_r": round(sum(rs) / len(rs), 3),
        "t_stat": round(mean_m / sd_m * math.sqrt(len(m)), 2) if sd_m > 0 else 0.0,  # on monthly sums
        "profit_factor": round(won / lost, 2) if lost > 0 else float("inf"),
        "win_pct": round(100 * sum(r > 0 for r in rs) / len(rs), 1),
    }


def p_value(t: float) -> float:
    return 0.5 * math.erfc(t / math.sqrt(2))


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    m, out, running = len(pvalues), {}, 0.0
    for rank, (key, p) in enumerate(sorted(pvalues.items(), key=lambda kv: kv[1])):
        running = max(running, min(1.0, (m - rank) * p))
        out[key] = round(running, 4)
    return out


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def evaluate(trades: list[Trade], plan: dict) -> dict:
    first_test, holdout = date.fromisoformat(str(plan["first_test"])), date.fromisoformat(str(plan["holdout_start"]))
    bar = plan["pass_bar"]
    rule = {"min_trades": 30, "min_avg_r": 0.0, "min_t_stat": 1.5, **(plan.get("selection") or {})}
    variants = sorted({t.variant for t in trades})
    by = {v: [t for t in trades if t.variant == v] for v in variants}

    def between(ts, a, b):
        return [t for t in ts if a <= t.entry_day < b]

    full = {v: stats(between(by[v], date.min, holdout)) for v in variants}
    adjusted = holm({v: p_value(s["t_stat"]) for v, s in full.items()})
    for v in full:
        full[v]["holm_p"] = adjusted[v]

    def chosen(a, b):
        return [v for v in variants if (s := stats(between(by[v], a, b)))["trades"] >= rule["min_trades"]
                and s["avg_r"] > rule["min_avg_r"] and s["t_stat"] >= rule["min_t_stat"]]

    windows, oos = [], []
    start = date(first_test.year, first_test.month, 1)
    while start < holdout:
        stop = min(_add_months(start, plan["test_months"]), holdout)
        picked = chosen(_add_months(start, -12 * plan["train_years"]), start)
        ts = [t for v in picked for t in between(by[v], start, stop) if t.exit_day < holdout]
        windows.append({"start": start.isoformat(), "variants": picked, **stats(ts)})
        oos.extend(ts)
        start = stop
    hold_variants = chosen(_add_months(holdout, -12 * plan["train_years"]), holdout)
    hold = stats([t for v in hold_variants for t in by[v] if t.entry_day >= holdout])
    o = stats(oos)
    traded = [w for w in windows if w["trades"]]
    positive = 100 * sum(w["avg_r"] > 0 for w in traded) / len(traded) if traded else 0.0
    best = min(full.items(), key=lambda kv: kv[1]["holm_p"]) if full else ("none", {"holm_p": 1.0})
    checks = [
        ("Out-of-sample trades", o["trades"], f">= {bar['min_trades']}", o["trades"] >= bar["min_trades"]),
        ("Average R per trade after costs", o["avg_r"], f">= {bar['min_avg_r']}", o["avg_r"] >= bar["min_avg_r"]),
        ("t-stat of monthly results", o["t_stat"], f">= {bar['min_t_stat']}", o["t_stat"] >= bar["min_t_stat"]),
        ("Profit factor", o["profit_factor"], f">= {bar['min_profit_factor']}", o["profit_factor"] >= bar["min_profit_factor"]),
        ("Windows with a profit (%)", round(positive, 1), f">= {bar['min_positive_windows_pct']}",
         positive >= bar["min_positive_windows_pct"]),
        ("Holdout year average R", hold["avg_r"], f">= {bar['holdout_min_avg_r']}",
         hold["trades"] > 0 and hold["avg_r"] >= bar["holdout_min_avg_r"]),
        (f"Not luck across {len(variants)} variants (Holm p, best: {best[0]})", best[1]["holm_p"], "<= 0.05",
         best[1]["holm_p"] <= 0.05),
    ]
    longs, shorts = [t for t in oos if t.side > 0], [t for t in oos if t.side < 0]
    return {
        "variants": full, "windows": windows, "out_of_sample": o, "holdout": {"variants": hold_variants, **hold},
        "longs": stats(longs), "shorts": stats(shorts),
        "pass_bar": [{"check": c, "got": g, "needs": n, "passed": bool(p)} for c, g, n, p in checks],
        "passed": all(p for *_, p in checks),
    }


def run(settings, data_dir: Path = DATA_DIR) -> dict:
    reports = json.loads((data_dir / "earnings.json").read_text())
    bench = load_bars(data_dir / f"{settings.benchmark}.csv")
    trades = []
    for symbol in settings.symbols:
        path = data_dir / f"{symbol}.csv"
        if not path.exists():
            continue
        times = [datetime.fromisoformat(t) for t in reports.get(symbol, [])]
        trades += simulate(symbol, load_bars(path), bench, times, settings.research)
    result = evaluate(trades, settings.research)
    result["reports_used"] = sum(len(v) for v in reports.values())
    return result


def promote(result: dict, path: Path = PROMOTED) -> bool:
    """Write the rule for the live quant seat only if every check passed; otherwise remove any old one."""
    if not result["passed"]:
        path.unlink(missing_ok=True)
        return False
    best = min(result["variants"].items(), key=lambda kv: kv[1]["holm_p"])
    threshold, hold = best[0][1:].split("_h")
    o = result["out_of_sample"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"post_earnings_drift": {
        "variant": best[0], "threshold_pct": float(threshold), "hold_days": int(hold), "avg_r": o["avg_r"],
        "trades": o["trades"], "t_stat": o["t_stat"], "confidence": 55,
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="minutes")}}, indent=1))
    return True


def render(result: dict) -> str:
    lines = ["# Post-earnings drift on the 20 largest US tech stocks", "",
             f"**Verdict: {'PASSED, promoted to the quant seat' if result['passed'] else 'FAILED, not used live'}.**", "",
             "Code-only test: 8-K item 2.02 earnings dates from SEC EDGAR, Yahoo daily prices adjusted for splits and "
             "dividends, entry at the next open, a 1.5x ATR stop, real costs. See `tradeboy/stocks/research.py` for the "
             "rules, which were fixed before the first run.", "",
             "Known bias: the stock list is today's winners, which flatters long trades. Longs and shorts are shown "
             "separately below.", "", "## Pass bar (out of sample)", "", "| Check | Result | Needs | |", "| --- | --- | --- | --- |"]
    for c in result["pass_bar"]:
        lines.append(f"| {c['check']} | {c['got']} | {c['needs']} | {'pass' if c['passed'] else 'FAIL'} |")
    o, lo, sh = result["out_of_sample"], result["longs"], result["shorts"]
    lines += ["", "## Out of sample, by side", "", "| | Trades | Avg R | t-stat | Win % |", "| --- | --- | --- | --- | --- |",
              f"| All | {o['trades']} | {o['avg_r']} | {o['t_stat']} | {o['win_pct']} |",
              f"| Longs | {lo['trades']} | {lo['avg_r']} | {lo['t_stat']} | {lo['win_pct']} |",
              f"| Shorts | {sh['trades']} | {sh['avg_r']} | {sh['t_stat']} | {sh['win_pct']} |",
              "", "## Every variant over the full history (before the holdout)", "",
              "| Variant | Trades | Avg R | t-stat | Holm p |", "| --- | --- | --- | --- | --- |"]
    for v, s in result["variants"].items():
        lines.append(f"| {v} | {s['trades']} | {s['avg_r']} | {s['t_stat']} | {s['holm_p']} |")
    lines += ["", "## Walk-forward windows", "", "| Start | Variants traded | Trades | Avg R |", "| --- | --- | --- | --- |"]
    for w in result["windows"]:
        lines.append(f"| {w['start']} | {', '.join(w['variants']) or 'none'} | {w['trades']} | {w['avg_r']} |")
    h = result["holdout"]
    lines += ["", f"Holdout year: {h['trades']} trades, average {h['avg_r']}R (variants: {', '.join(h['variants']) or 'none'}).", ""]
    return "\n".join(lines)

