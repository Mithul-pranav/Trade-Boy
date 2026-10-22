"""Turns a macro_backtest.run() result into reports/macro_backtest.md, in plain language."""

from __future__ import annotations

from . import macro_backtest as mb


def _pct(x: float) -> str:
    return f"{100 * x:+.1f}%"


def _label(key: str) -> str:
    if key == "ridge":
        return "Ridge regression on all features (machine learning, refitted every 6 months)"
    rule, _, ex = key.partition("+")
    text = mb.RULES_BY_NAME[rule].description
    return f"{text}; exit: {mb.EXIT_RULES_BY_NAME[ex].description.lower()}" if ex else text


def render(r: dict) -> str:
    plan = r["plan"]
    lines = [
        "# Macro strategy backtest (weekly, 2004 onwards)",
        "",
        f"Generated {r['generated']} by `tradeboy macro-backtest`. Pairs: {', '.join(mb.PAIRS)}. Weekly positions "
        f"from {r['first_week']} to {r['last_week']}, executed at the first 4h OANDA candle from Monday 09:00 UTC "
        "with that candle's real bid and ask.",
        "",
        f"## Verdict: {'PASSED' if r['passed'] else 'NOT PASSED'}",
        "",
        "| Check | Needs | Got | |",
        "|---|---|---|---|",
    ]
    for c in r["pass_bar"]:
        lines.append(f"| {c['check']} | {c['needs']} | {c['got']} | {'pass' if c['passed'] else 'FAIL'} |")
    lines += [
        "",
        "## How to read this",
        "",
        "- **Sharpe ratio** is yearly return divided by yearly ups and downs. 0 means no edge; 0.5 is a modest "
        "real edge; a good macro hedge fund aims for about 1.",
        "- **t-stat** says how sure we can be that the average is above zero and not luck; about 2 or more is the "
        "usual bar. **Holm p** corrects for testing many strategies at once: with "
        f"{len(r['full'])} strategies, some will look good by chance. Below 0.05 would count.",
        "- **At 10% volatility** means the strategy scaled so its yearly ups and downs are about 10% of capital. "
        "On 100 USD that is roughly a 10 USD swing in a typical year.",
        "- **Costs** are all in: half the real spread each time a position changes, 0.2 pips slippage each side, "
        f"and overnight financing (the interest rate gap minus OANDA's {plan.costs.financing_markup_pct}% a year).",
        "- Positions are sized so each pair has the same risk (10% yearly volatility on its share of capital).",
        "- Rates: OECD monthly 3-month and 10-year rates on FRED, used only from the month after next. Where a "
        f"3-month series is more than {mb.STALE_DAYS} days stale, the daily policy rate is used (Fed, ECB, BoE, "
        "RBA, BoC). Two-year yields for all 8 currencies are not available free, so 3-month rates stand in for "
        "short-term rate expectations.",
        "",
        "## 1. Every strategy over the development years (2006 to October 2025)",
        "",
        "Fixed rules, nothing fitted (except the ridge regression, which is refitted only on past data). "
        "'Exit' variants hold one trade at a time per pair and book each trade in the week it closes.",
        "",
        "| Strategy | Sharpe | t-stat | Holm p | Yearly at 10% vol | Worst drop | Higher costs Sharpe | "
        "2006-09 | 2010-14 | 2015-19 | 2020-25 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    order = sorted(r["full"], key=lambda k: -r["full"][k]["t_stat"])
    for k in order:
        s, e = r["full"][k], r["by_era"][k]
        lines.append(
            f"| {_label(k)} | {s['sharpe']:+.2f} | {s['t_stat']:+.2f} | {r['adjusted_p'][k]:.2f} | "
            f"{_pct(s['ann_return_at_target'])} | {100 * s['max_drawdown_at_target']:.0f}% | "
            f"{r['full_stressed'][k]['sharpe']:+.2f} | " + " | ".join(f"{v:+.2f}" for v in e.values()) + " |")
    best = order[0]
    lines += [
        "",
        f"Best by t-stat: **{_label(best)}** (t = {r['full'][best]['t_stat']}, Holm p = "
        f"{r['adjusted_p'][best]:.2f}). The Holm p says whether that is more than the luck expected from trying "
        f"{len(r['full'])} strategies.",
        "",
        "## 2. Exits, head to head on the same entries",
        "",
        "Each rule's entries are the weeks its signal switches on or changes side. Every exit rule is applied to "
        "exactly those entries, so the only difference is the exit. Results are per trade, in **R**, where 1R is "
        "two typical weekly moves (the stop distance used by the stop exits). Random entries (a coin flip for "
        "the side, about one new trade every 8 weeks per pair, 10 different coins) show what exits do when the "
        "entry has no edge.",
        "",
        "| Entries | Exit | Trades | Average per trade | t-stat | Won | Average hold (weeks) |",
        "|---|---|---|---|---|---|---|",
    ]
    for x in r["exits"]:
        extra = ""
        if x["entries"] == "random":
            extra = f" (coins ranged {x['worst'] / 2:+.3f}R to {x['best'] / 2:+.3f}R)"
        lines.append(f"| {x['entries']} | {mb.EXIT_RULES_BY_NAME[x['exit']].description} | {x['trades']:.0f} | "
                     f"{x['avg_risk_units'] / 2:+.3f}R{extra} | {x['t_stat']:+.2f} | {x['won_pct']:.0f}% | "
                     f"{x['avg_weeks']:.1f} |")
    rand = [x for x in r["exits"] if x["entries"] == "random"]
    real = [x for x in r["exits"] if x["entries"] != "random"]
    top = max(real, key=lambda x: x["t_stat"])
    key = f"{top['entries']}+{top['exit']}" if top["exit"] != "flip" else top["entries"]
    tradeable = r["full"].get(key)
    if tradeable:
        lines += [
            "",
            f"The strongest result here, {top['entries']} with the '{top['exit']}' exit (t = {top['t_stat']:+.2f}), is "
            f"the best of {len(real)} rule and exit pairs, so a t-stat that high is expected from luck alone about "
            f"{min(100, round(100 * len(real) * mb.p_value(top['t_stat'])))}% of the time. Traded for real (one "
            f"position per pair at a time, section 1) it has a Sharpe of {tradeable['sharpe']:+.2f}.",
        ]
    lines += [
        "",
        "With random entries every exit loses a little: "
        + ", ".join(f"{mb.EXIT_RULES_BY_NAME[x['exit']].name} {x['avg_risk_units'] / 2:+.3f}R" for x in rand)
        + ". An exit changes how often you win and how big wins and losses are, but on its own it does not "
        "create an edge; it can only keep more of one the entry already has.",
        "",
        "## 3. Central bank rate moves",
        "",
        f"{len(r['decisions'])} rate changes by the Fed, ECB, BoE, RBA and BoC since 2004 (daily records for the "
        "BoJ, SNB and RBNZ are not reachable from here). After each one, trade the currency in the direction of "
        "the move against the dollar from the next Monday, after costs, in units of the hold period's typical move:",
        "",
        "| Hold | Events | Average | Won | t-stat |",
        "|---|---|---|---|---|",
    ]
    for e in r["events"]:
        lines.append(f"| {e['hold_weeks']} week(s) | {e['events']} | {e['avg_vol_units']:+.3f} | {e['won_pct']}% | "
                     f"{e['t_stat']:+.2f} |")
    lines += [
        "",
        "Rate moves are mostly expected by the market before they happen, so the move itself carries little news. "
        "The surprise (the move versus what was priced) is what moves currencies, and testing that needs market "
        "pricing data that is not free.",
        "",
        "## 4. Walk-forward (the honest test)",
        "",
        f"Every 6 months, trade the strategies whose t-stat over all earlier weeks (at least 5 years) was at least "
        f"{plan.min_t_stat}, then judge them on the 6 months they were not chosen on. Strategies in the pool: "
        f"{len(r['full'])}.",
        "",
        "| Test window | Chosen strategies | Return at 10% vol |",
        "|---|---|---|",
    ]
    for w in r["windows"]:
        chosen = ", ".join(w["chosen"]) if w["chosen"] else "none qualified"
        lines.append(f"| {w['start']} to {w['end']} | {chosen} | {_pct(w['return'])} |")
    o, os_ = r["oos"], r["oos_stressed"]
    lines += [
        "",
        f"Out of sample: Sharpe {o['sharpe']:+.2f}, t-stat {o['t_stat']:+.2f}, {_pct(o['ann_return_at_target'])} a "
        f"year at 10% volatility, worst drop {100 * o['max_drawdown_at_target']:.0f}%. With higher costs: Sharpe "
        f"{os_['sharpe']:+.2f}.",
        "",
        f"Holdout year (from {plan.holdout_start.date()}): chosen on everything before it: "
        f"{', '.join(r['holdout']['chosen']) or 'nothing qualified'}; result {_pct(r['holdout']['return'])}.",
        "",
        "## 5. What Claude debates would cost against what this makes",
        "",
        f"At {plan.capital_usd:.0f} USD and 10% volatility, using the out-of-sample result, with a debate at "
        f"{plan.debate_cost_usd} USD:",
        "",
        "| Debate design | Debates a year | API cost | Expected trading profit | Net | Capital needed to break even |",
        "|---|---|---|---|---|---|",
    ]
    for e in r["economics"]:
        be = f"{e['capital_to_break_even_usd']:,} USD" if e["capital_to_break_even_usd"] else "never (no edge)"
        lines.append(f"| {e['design']} | {e['debates_per_year']} | {e['api_cost_usd']:.2f} USD | "
                     f"{e['expected_profit_usd']:+.2f} USD | {e['net_usd']:+.2f} USD | {be} |")
    lines += [
        "",
        "Whether a debate adds edge on top of a rule can't be backtested honestly: Claude has read about these "
        "years, so a debate replayed on 2015 data already knows what happened next. It can only be measured going "
        "forward, by logging debate calls next to the plain rule's calls.",
        "",
    ]
    lines += _open_trades(r)
    return "\n".join(lines)


REVIEW_COST_USD = 0.05  # an estimate for a short two-agent check, not measured; a full debate measured 0.11-0.21


def _open_trades(r: dict) -> list[str]:
    """How the desk enters and exits today, and a proposal for managing open trades, with its cost."""
    d = r["decisions"]
    years = max((d["date"].max() - d["date"].min()).days / 365.25, 1)
    per_pair = {p: sum(d["currency"].isin(p.split("_"))) / years for p in mb.PAIRS}
    avg_moves = sum(per_pair.values()) / len(per_pair)
    open_trades, weekly_checks = 3, 52
    reviews = open_trades * (avg_moves / 2 + 12)  # half of the moves go against a trade; ~1 other trigger a month
    return [
        "## 6. How the desk gets in and out today",
        "",
        "- **In:** after a debate, a market order goes in straight away. No limit price, no waiting for a better level.",
        "- **Out:** the head of desk sets a stop loss (always) and maybe a take-profit when the trade opens. OANDA "
        "closes the trade when either is hit. The agents also give a 'hold for N hours', but the code only uses it to "
        "score their calls, never to close a trade.",
        "- **After entry, nobody looks again.** No agent re-reviews an open trade when news breaks, a central bank "
        "moves, or the reason for the trade goes away. The only other exit is the kill switch (budget gone).",
        "",
        "## 7. Proposal: managing open trades",
        "",
        "What the tests above say about exits:",
        "",
        "1. Exits can't rescue a trade idea with no edge. On random entries every exit rule lost about the cost of "
        "trading (section 2).",
        "2. No single exit was best for every rule. The right exit depends on why the trade was opened, so it should be "
        "decided at entry and written down, not improvised later.",
        "3. Every trade still needs a hard stop at OANDA, because the desk can be offline and markets gap.",
        "",
        "So the proposal is a cheap code check with an agent re-review only when something has changed:",
        "",
        "| Layer | What it does | Cost |",
        "|---|---|---|",
        "| Stop at OANDA | Every trade keeps its hard stop, set at entry (as today) | free |",
        "| Exit plan at entry | The head of desk must name the exit type (time limit, signal flip, trailing stop, or "
        "target) and the reason the trade exists, stored with the trade | already part of the debate |",
        "| Code check every scan | Closes the trade when its exit plan says so: time limit reached, the rule that "
        "opened it has flipped, or a trailing stop is hit | free |",
        "| Agent re-review on triggers only | A short two-agent check (hold or close) when a central bank moves against "
        "the trade, the price moves more than two typical weekly moves against it, or major news hits the pair | "
        f"about {REVIEW_COST_USD} USD each (estimate) |",
        "",
        f"Central banks for the 7 pairs moved rates about {avg_moves:.1f} times a year per pair since 2004. With "
        f"{open_trades} trades open, roughly half of those moves going against a trade, and about one other trigger a "
        f"month per trade, that is about {reviews:.0f} re-reviews a year, or about {reviews * REVIEW_COST_USD:.0f} USD "
        "a year of API credits. Re-reviewing every open trade every hour instead would cost "
        f"about {open_trades * 24 * 260 * REVIEW_COST_USD:,.0f} USD a year.",
        "",
        "The trigger list itself is untested: central bank moves against a trade made no reliable difference here "
        "(section 2, 'central bank' exits), so this layer is a safety net for surprises, not a source of profit.",
        "",
    ]
