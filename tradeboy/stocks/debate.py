"""One debate on one stock event: independent views, rebuttals, the skeptic, the risk manager, the head.

Same rules as the forex desk, enforced in code: analysts commit before seeing each other, a stance change
must name the argument behind it, unanswered serious objections halve the size, and so does trading
against a tested quant signal. The risk quant's limit caps size on top of the risk manager's.
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import Side
from . import agents
from .market import move_today, ny_date, recent_bars, summarize
from .models import Bar, StockEvent, StockProposal, StockQuote
from .sec import describe

ANALYSTS = {
    "fundamentals": (agents.FUNDAMENTALS, agents.VIEW_SCHEMA, 0),
    "news": (agents.NEWS, agents.VIEW_SCHEMA, 1),
    "technical": (agents.TECHNICAL, agents.TECHNICAL_SCHEMA, 0),
}
MIN_SIZE = 0.25
MIN_TRADE_SIZE = 0.1


@dataclass
class StockEvidence:
    symbol: str
    name: str
    group: str
    quote: StockQuote
    event: StockEvent
    other_events: list[StockEvent] = field(default_factory=list)
    daily: list[Bar] = field(default_factory=list)
    hourly: list[Bar] = field(default_factory=list)
    benchmark: str = "QQQ"
    benchmark_daily: list[Bar] = field(default_factory=list)
    reaction: dict | None = None
    filing_text: str = ""
    recent_filings: list[str] = field(default_factory=list)
    next_earnings: str | None = None
    news: list[dict] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    risk_quant: dict | None = None


@dataclass
class StockDebateResult:
    debate_id: str
    symbol: str
    proposal: StockProposal | None
    outcome: str
    transcript: dict
    initial_stances: dict[str, str]
    final_stances: dict[str, str]
    stance_changes: int
    max_share: float = 1.0


def gather(settings, broker, event: StockEvent, now: datetime, *, edgar=None, news=None, earnings=None,
           other_events=(), risk_quant=None, open_positions=(), nav: float = 0.0) -> StockEvidence:
    symbol = event.symbol
    quote = broker.quotes([symbol, settings.benchmark])
    ev = StockEvidence(symbol, settings.name(symbol), settings.group(symbol), quote[symbol], event, list(other_events),
                       benchmark=settings.benchmark)
    ev.daily = broker.bars(symbol, "1Day", 120)
    ev.hourly = broker.bars(symbol, "1Hour", 100)
    ev.benchmark_daily = broker.bars(settings.benchmark, "1Day", 120)
    if settings.benchmark in quote:
        ev.reaction = move_today(ev.daily, ev.benchmark_daily, ev.quote.mid, quote[settings.benchmark].mid, ny_date(now))
    else:
        ev.data_gaps.append(f"No live quote for the benchmark {settings.benchmark}.")
    if edgar is not None:
        filing = event.filing or next((e.filing for e in other_events if e.filing), None)
        if filing is not None:
            try:
                ev.filing_text = edgar.filing_text(filing)
            except Exception as exc:
                ev.data_gaps.append(f"Could not read the filing text: {exc}")
        ev.recent_filings = [describe(f) for f in edgar.filings(symbol, now, days=120)[:10]]
        ev.data_gaps.extend(edgar.gaps(symbol))
    else:
        ev.data_gaps.append("SEC filings are not connected.")
    if earnings is not None:
        nxt = earnings.next_earnings(symbol, now)
        ev.next_earnings = nxt.isoformat() if nxt else None
        if nxt is None:
            ev.data_gaps.append("The next earnings date is unknown.")
    if news is not None:
        ev.news = [{"time": h.time.isoformat(timespec="minutes"), "source": h.publisher or h.source, "headline": h.title}
                   for h in news.headlines({symbol}, now, hours=72, limit=25)]
        ev.data_gaps.extend(news.gaps({symbol}))
    if risk_quant is not None:
        daily = {symbol: ev.daily}
        for p in open_positions:
            if p["symbol"] != symbol:
                daily[p["symbol"]] = broker.bars(p["symbol"], "1Day", 120)
        positions = [(p["symbol"], 1 if p["qty"] > 0 else -1, p["risk"]) for p in open_positions]
        ev.risk_quant = risk_quant.assess(symbol, nav, positions, daily)
    return ev


def _dump(data) -> str:
    return json.dumps(data, indent=1, default=str)


def _clamp(value, low=0, high=100):
    return max(low, min(high, value))


class StockDebate:
    def __init__(self, settings, llm, track_records: dict | None = None, quant=None):
        self.settings = settings
        self.claude = settings.claude
        self.llm = llm
        self.track_records = track_records or {}
        self.quant = quant

    def _ask(self, agent, system, prompt, schema, searches=0, head=False):
        return self.llm.ask(
            agent=agent, system=system, prompt=prompt, schema=schema, web_searches=searches,
            model=self.claude.head_model if head else self.claude.analyst_model,
            effort=self.claude.head_effort if head else self.claude.analyst_effort,
        )

    def _header(self, ev: StockEvidence, now: datetime) -> str:
        quote = {"bid": ev.quote.bid, "ask": ev.quote.ask, "last": ev.quote.last, "spread_pct": round(ev.quote.spread_pct, 3)}
        others = [e.summary for e in ev.other_events if e.key != ev.event.key]
        return (
            f"Stock: {ev.symbol} ({ev.name}, group: {ev.group})\nTime (UTC): {now.isoformat(timespec='minutes')}\n"
            f"Quote: {_dump(quote)}\nWhy this debate was called ({ev.event.kind}): {ev.event.summary}\n"
            + (f"Other new events on this stock: {_dump(others)}\n" if others else "")
            + f"Price reaction today vs the previous close and vs {ev.benchmark}: {_dump(ev.reaction) if ev.reaction else 'unknown'}\n"
            f"Next scheduled earnings: {ev.next_earnings or 'unknown'}\n"
        )

    def _analyst_prompt(self, name: str, ev: StockEvidence, now: datetime) -> str:
        head = self._header(ev, now)
        gaps = f"\nMissing data (treat as unknown, not as calm):\n{_dump(ev.data_gaps)}\n" if ev.data_gaps else ""
        if name == "technical":
            charts = {
                "daily": {"summary": summarize(ev.daily, ev.benchmark_daily), "last_15_bars": recent_bars(ev.daily, 15, "%Y-%m-%d")},
                "hourly": {"summary": summarize(ev.hourly), "last_14_bars": recent_bars(ev.hourly, 14)},
                f"{ev.benchmark}_daily": summarize(ev.benchmark_daily),
            }
            return head + f"\nCharts (rows are [New York time, open, high, low, close, volume]):\n{_dump(charts)}"
        if name == "fundamentals":
            text = ev.filing_text or "No filing text for this event."
            return (head + f"\nThe company's recent SEC filings:\n{_dump(ev.recent_filings)}\n"
                    f"\nText of the filing behind this debate (cut to length):\n<<<\n{text}\n>>>\n" + gaps)
        news = _dump(ev.news) if ev.news else "None found."
        return head + f"\nHeadlines on the company (last 3 days, newest first):\n{news}\n" + gaps

    def run(self, ev: StockEvidence, nav: float, open_positions: list[dict], now: datetime) -> StockDebateResult:
        debate_id = f"{now.strftime('%Y%m%d-%H%M')}-{ev.symbol}-{uuid.uuid4().hex[:6]}"
        transcript: dict = {
            "symbol": ev.symbol, "time": now.isoformat(), "event": {"kind": ev.event.kind, "summary": ev.event.summary},
            "evidence": {
                "quote": {"bid": ev.quote.bid, "ask": ev.quote.ask}, "reaction": ev.reaction,
                "daily": summarize(ev.daily, ev.benchmark_daily) if ev.daily else None,
                "next_earnings": ev.next_earnings, "recent_filings": ev.recent_filings, "news": ev.news,
                "filing_text_chars": len(ev.filing_text), "data_gaps": ev.data_gaps,
            },
        }

        # 1. Think alone first: all three at once, none sees another's view.
        def first_view(name):
            system, schema, searches = ANALYSTS[name]
            return name, self._ask(name, system, self._analyst_prompt(name, ev, now), schema,
                                   searches=searches * self.claude.web_searches_per_agent)

        with ThreadPoolExecutor(max_workers=len(ANALYSTS)) as pool:
            views = dict(pool.map(first_view, ANALYSTS))
        for view in views.values():
            view["confidence"] = _clamp(view["confidence"])
        transcript["independent_views"] = views

        quant = self.quant.view(ev.symbol, ev.event.kind, ev.reaction) if self.quant is not None else None
        if quant:
            transcript["quant_seat"] = quant
        quant_side = quant["stance"] if quant and quant["significant"] else None

        # 2. Rebuttals, unless all three (and any tested quant signal) already agree.
        def rebut(name):
            system, _, _ = ANALYSTS[name]
            others = {k: v for k, v in views.items() if k != name}
            prompt = (f"{self._analyst_prompt(name, ev, now)}\n\nYour independent view:\n{_dump(views[name])}\n\n"
                      f"The other analysts' independent views:\n{_dump(others)}\n"
                      + (f"\nThe quant seat (statistics computed in code):\n{_dump(quant)}\n" if quant else "")
                      + agents.REBUTTAL)
            return name, self._ask(f"{name}_rebuttal", system, prompt, agents.REBUTTAL_SCHEMA)

        if len({v["stance"] for v in views.values()} | ({quant_side} if quant_side else set())) > 1:
            with ThreadPoolExecutor(max_workers=len(ANALYSTS)) as pool:
                rebuttals = dict(pool.map(rebut, ANALYSTS))
        else:
            rebuttals = {n: {"skipped": "All analysts agreed independently.", "final_stance": v["stance"],
                             "final_confidence": v["confidence"], "changed_by": ""} for n, v in views.items()}
        changes = 0
        for name, reb in rebuttals.items():
            reb["final_confidence"] = _clamp(reb["final_confidence"])
            if reb["final_stance"] != views[name]["stance"]:
                if not reb["changed_by"].strip():
                    reb["note"] = "Stance change ignored: it didn't name the argument that caused it."
                    reb["final_stance"], reb["final_confidence"] = views[name]["stance"], views[name]["confidence"]
                else:
                    changes += 1
        transcript["rebuttals"] = rebuttals
        final = {n: {"stance": rebuttals[n]["final_stance"], "confidence": rebuttals[n]["final_confidence"]} for n in ANALYSTS}

        # 3. The skeptic, whatever the analysts concluded.
        case = {
            "event": transcript["event"], "independent_views": views, "rebuttals": rebuttals, "final_positions": final,
            **({"quant_seat": quant} if quant else {}),
            "quote": {"bid": ev.quote.bid, "ask": ev.quote.ask}, "reaction_today": ev.reaction,
            "daily_atr14_pct": summarize(ev.daily)["atr14_pct"] if ev.daily else None,
            "next_earnings": ev.next_earnings, "data_gaps": ev.data_gaps,
        }
        skeptic = self._ask("skeptic", agents.SKEPTIC, f"Stock: {ev.symbol} ({ev.name})\nThe case so far:\n{_dump(case)}",
                            agents.SKEPTIC_SCHEMA)
        transcript["skeptic"] = skeptic

        # 4. The risk manager, with the risk quant's measurements; the quant's limit is enforced below.
        risk = self._ask("risk_manager", agents.RISK, self._risk_prompt(ev, nav, open_positions), agents.RISK_SCHEMA)
        risk["long_size"] = _clamp(risk["long_size"], MIN_SIZE, 1.0)
        risk["short_size"] = _clamp(risk["short_size"], MIN_SIZE, 1.0)
        transcript["risk_manager"] = risk
        if ev.risk_quant:
            transcript["risk_quant"] = ev.risk_quant

        # 5. The head of desk.
        head_prompt = (
            f"Stock: {ev.symbol} ({ev.name})\nThe full debate:\n"
            f"{_dump({**case, 'skeptic': skeptic, 'risk_manager': risk, 'risk_quant': ev.risk_quant})}\n\n"
            f"Analyst track records (calls scored after their time horizon, in basis points of price):\n"
            f"{_dump(self.track_records) if self.track_records else 'None scored yet: weigh arguments only.'}"
            + ("" if self.settings.allow_shorts else
               "\n\nShorts are switched off on this desk: a short call is logged and no order is placed.")
        )
        head = self._ask("head_of_desk", agents.HEAD, head_prompt, agents.HEAD_SCHEMA, head=True)
        transcript["head_of_desk"] = head

        outcome, proposal, max_share = self._enforce(ev, debate_id, skeptic, risk, head, quant_side)
        transcript["outcome"] = outcome
        return StockDebateResult(debate_id, ev.symbol, proposal, outcome, transcript,
                                 {k: v["stance"] for k, v in views.items()}, {k: v["stance"] for k, v in final.items()},
                                 changes, max_share)

    def _risk_prompt(self, ev: StockEvidence, nav: float, open_positions: list[dict]) -> str:
        limits = self.settings.risk
        rows = [{"symbol": p["symbol"], "group": self.settings.group(p["symbol"]), "side": "long" if p["qty"] > 0 else "short",
                 "value_usd": round(p["value"], 2), "risk_at_stop_usd": round(p["risk"], 2)} for p in open_positions]
        return (
            f"Proposed stock: {ev.symbol} ({ev.name}, group {ev.group})\nBudget equity: {nav:,.2f} USD\n"
            f"Normal risk per trade {limits.risk_per_trade_pct}% of budget; total open risk cap {limits.max_total_risk_pct}%; "
            f"max {limits.max_open_positions} positions; one position at most {limits.max_position_pct}% of budget; "
            f"daily loss limit {limits.daily_loss_limit_pct}%.\nOpen positions:\n{_dump(rows) if rows else 'None'}\n"
            f"Next earnings: {ev.next_earnings or 'unknown'}\n"
            f"Daily ATR14: {summarize(ev.daily)['atr14_pct'] if ev.daily else 'unknown'}%\n"
            f"Risk quant (measured in code):\n{_dump(ev.risk_quant) if ev.risk_quant else 'not available'}"
        )

    def _enforce(self, ev: StockEvidence, debate_id: str, skeptic: dict, risk: dict, head: dict, quant_side):
        side = Side.BUY if head["side"] == "long" else Side.SELL
        answered = {a["objection_id"] for a in head["objection_answers"] if a["resolved"]}
        open_objections = [o["id"] for o in skeptic["objections"]
                           if o["severity"] in ("fatal", "serious") and o["id"] not in answered]
        allowed = risk["long_size"] if side is Side.BUY else risk["short_size"]
        against_quant = quant_side is not None and head["side"] != quant_side
        halvings = len(open_objections) + against_quant
        size = max(allowed * _clamp(head["size"], MIN_SIZE, 1.0) * 0.5 ** halvings, MIN_TRADE_SIZE)
        max_share = 1.0
        if ev.risk_quant:
            max_share = ev.risk_quant["sides"]["long" if side is Side.BUY else "short"]["max_share"]
        stop, stop_note = self._stop(ev, side, head["stop_loss"])
        entry = ev.quote.ask if side is Side.BUY else ev.quote.bid
        target = head["take_profit"]
        if target is not None and (target - entry) * (1 if side is Side.BUY else -1) <= 0:
            target = None
        plan = head.get("exit_plan") or {"type": "time_limit", "why_trade_exists": head["reasoning"][:300], "close_early_if": "not stated"}
        proposal = StockProposal(
            symbol=ev.symbol, side=side, stop_loss=stop, take_profit=round(float(target), 2) if target is not None else None,
            rationale=head["reasoning"], debate_id=debate_id, risk_fraction=round(min(size, max_share), 3),
            hold_days=int(_clamp(int(head["hold_days"]), 1, self.settings.max_hold_days)),
            exit_type=plan["type"] if plan["type"] != "target" or target is not None else "time_limit",
            exit_reason=f"Why: {plan['why_trade_exists']} Close early if: {plan['close_early_if']}",
        )
        notes = [f"{head['side']} at {proposal.risk_fraction:.0%} size"]
        if open_objections:
            notes.append(f"halved for unanswered objections {', '.join(open_objections)}")
        if against_quant:
            notes.append("halved for going against the quant seat's tested signal")
        if size > max_share:
            notes.append(f"cut to the risk quant's {max_share:.0%} limit")
        if stop_note:
            notes.append(stop_note)
        return "trade: " + "; ".join(notes), proposal, max_share

    def _stop(self, ev: StockEvidence, side: Side, stop: float | None) -> tuple[float, str]:
        """The head's stop if usable; otherwise 1.5x the daily ATR. Every order has a stop."""
        entry = ev.quote.ask if side is Side.BUY else ev.quote.bid
        sign = -1 if side is Side.BUY else 1
        limits = self.settings.risk
        low, high = entry * (limits.min_stop_pct + 0.1) / 100, entry * (limits.max_stop_pct - 0.1) / 100
        if stop is not None and low <= (stop - entry) * sign <= high:
            return round(float(stop), 2), ""
        atr = summarize(ev.daily)["atr14_pct"] / 100 * entry if len(ev.daily) > 15 else entry * 0.03
        distance = min(max(1.5 * atr, low), high)
        if stop is not None and (stop - entry) * sign > 0:
            note = f"stop moved inside the allowed {limits.min_stop_pct:g}% to {limits.max_stop_pct:g}% band"
        else:
            note = "stop set from volatility (1.5x daily ATR) because the head's stop was unusable"
        return round(entry + sign * distance, 2), note
