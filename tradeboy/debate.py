"""One debate on one pair: independent views, rebuttals, the skeptic, the risk manager, then the head of desk.

The prompts ask the agents to reason instead of agreeing, and this code enforces the parts that can be
checked: analysts can't see each other before committing, a stance change must name the argument behind
it, and a trade can't go ahead while a fatal or serious objection is unanswered.
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from . import agents
from .config import Settings
from .llm import LLM
from .market import recent_bars, summarize
from .models import AccountState, Quote, Side, TradeProposal, currencies

ANALYSTS = {
    "technical": (agents.TECHNICAL, agents.TECHNICAL_SCHEMA, 0),
    "macro": (agents.MACRO, agents.VIEW_SCHEMA, 1),
    "positioning": (agents.POSITIONING, agents.VIEW_SCHEMA, 1),
}
TIMEFRAMES = {"M15": 200, "H1": 200, "H4": 200, "D": 100}


@dataclass
class Evidence:
    """What each analyst gets to look at. Each one gets a different slice, so they bring different facts."""

    instrument: str
    quote: Quote
    charts: dict[str, dict] = field(default_factory=dict)
    position_book: dict | None = None
    calendar: list[dict] = field(default_factory=list)
    scanner_reason: str = ""


@dataclass
class DebateResult:
    debate_id: str
    instrument: str
    proposal: TradeProposal | None
    outcome: str  # "trade" or why there's no trade
    transcript: dict
    initial_stances: dict[str, str]
    final_stances: dict[str, str]
    stance_changes: int


def gather_evidence(broker, calendar, instrument: str, now: datetime, scanner_reason: str = "") -> Evidence:
    quote = broker.quotes([instrument])[instrument]
    evidence = Evidence(instrument, quote, scanner_reason=scanner_reason)
    for granularity, count in TIMEFRAMES.items():
        candles = broker.candles(instrument, granularity, count)
        evidence.charts[granularity] = {"summary": summarize(instrument, candles), "recent_bars": recent_bars(candles)}
    try:
        evidence.position_book = broker.position_book(instrument)
    except Exception as exc:  # positioning data is helpful, not essential
        evidence.position_book = {"unavailable": str(exc)[:200]}
    if calendar is not None and hasattr(calendar, "upcoming"):
        evidence.calendar = [
            {"time": e.time.isoformat(), "currency": e.currency, "event": e.title, "impact": e.impact}
            for e in calendar.upcoming(set(currencies(instrument)), now)
        ]
    return evidence


def _dump(data) -> str:
    return json.dumps(data, indent=1, default=str)


def _clamp(value, low=0, high=100):
    return max(low, min(high, value))


class Debate:
    def __init__(self, settings: Settings, llm: LLM, track_records: dict | None = None):
        self.settings = settings
        self.claude = settings.claude
        self.llm = llm
        self.track_records = track_records or {}

    def _ask(self, agent: str, system: str, prompt: str, schema: dict, searches: int = 0, head: bool = False):
        return self.llm.ask(
            agent=agent, system=system, prompt=prompt, schema=schema, web_searches=searches,
            model=self.claude.head_model if head else self.claude.analyst_model,
            effort=self.claude.head_effort if head else self.claude.analyst_effort,
        )

    def _analyst_prompt(self, name: str, ev: Evidence, now: datetime) -> str:
        quote = {"bid": ev.quote.bid, "ask": ev.quote.ask, "spread_pips": round(ev.quote.spread_pips, 1)}
        head = f"Pair: {ev.instrument}\nTime (UTC): {now.isoformat(timespec='minutes')}\nQuote: {_dump(quote)}\n"
        if ev.scanner_reason:
            head += f"Why the scanner flagged it: {ev.scanner_reason}\n"
        if name == "technical":
            return head + f"\nCharts by timeframe (summary plus the last 12 bars, mid prices):\n{_dump(ev.charts)}"
        if name == "macro":
            cal = _dump(ev.calendar) if ev.calendar else "No calendar feed available; search for upcoming events."
            return head + f"\nScheduled events for these currencies (next 48 hours):\n{cal}"
        return head + f"\nOANDA retail position book:\n{_dump(ev.position_book)}"

    def run(self, ev: Evidence, account: AccountState, now: datetime) -> DebateResult:
        debate_id = f"{now.strftime('%Y%m%d-%H%M')}-{ev.instrument}-{uuid.uuid4().hex[:6]}"
        transcript: dict = {
            "instrument": ev.instrument,
            "time": now.isoformat(),
            "evidence": {
                "quote": {"bid": ev.quote.bid, "ask": ev.quote.ask},
                "charts": {tf: c["summary"] for tf, c in ev.charts.items()},
                "position_book": ev.position_book,
                "calendar": ev.calendar,
                "scanner_reason": ev.scanner_reason,
            },
        }

        # 1. Think alone first: all three analysts at once, none sees another's view.
        def first_view(name):
            system, schema, searches = ANALYSTS[name]
            return name, self._ask(name, system, self._analyst_prompt(name, ev, now), schema,
                                   searches=searches * self.claude.web_searches_per_agent)

        with ThreadPoolExecutor(max_workers=len(ANALYSTS)) as pool:
            views = dict(pool.map(first_view, ANALYSTS))
        for view in views.values():
            view["confidence"] = _clamp(view["confidence"])
        transcript["independent_views"] = views

        # 2. Rebuttal round: each reads the others and must answer the strongest argument against it.
        # When all three already agree there is no opposing analyst to answer, so the round is skipped
        # to save cost; the skeptic still attacks the shared view.
        def rebut(name):
            system, _, _ = ANALYSTS[name]
            others = {k: v for k, v in views.items() if k != name}
            prompt = (
                f"{self._analyst_prompt(name, ev, now)}\n\nYour independent view:\n{_dump(views[name])}\n\n"
                f"The other analysts' independent views:\n{_dump(others)}\n{agents.REBUTTAL}"
            )
            return name, self._ask(f"{name}_rebuttal", system, prompt, agents.REBUTTAL_SCHEMA)

        if len({v["stance"] for v in views.values()}) > 1:
            with ThreadPoolExecutor(max_workers=len(ANALYSTS)) as pool:
                rebuttals = dict(pool.map(rebut, ANALYSTS))
        else:
            rebuttals = {
                name: {"skipped": "All analysts agreed independently.", "final_stance": v["stance"],
                       "final_confidence": v["confidence"], "changed_by": ""}
                for name, v in views.items()
            }
        changes = 0
        for name, reb in rebuttals.items():
            reb["final_confidence"] = _clamp(reb["final_confidence"])
            if reb["final_stance"] != views[name]["stance"]:
                if not reb["changed_by"].strip():
                    # A flip with no named argument is social pressure, not persuasion: keep the original view.
                    reb["note"] = "Stance change ignored: it didn't name the argument that caused it."
                    reb["final_stance"] = views[name]["stance"]
                    reb["final_confidence"] = views[name]["confidence"]
                else:
                    changes += 1
        transcript["rebuttals"] = rebuttals
        final = {
            name: {"stance": rebuttals[name]["final_stance"], "confidence": rebuttals[name]["final_confidence"]}
            for name in ANALYSTS
        }

        # 3. The skeptic argues against the trade, whatever the analysts concluded.
        case = {
            "independent_views": views,
            "rebuttals": rebuttals,
            "final_positions": final,
            "quote": {"bid": ev.quote.bid, "ask": ev.quote.ask},
            "volatility_atr14_pips": {tf: c["summary"]["atr14_pips"] for tf, c in ev.charts.items()},
            "scheduled_events": ev.calendar,
        }
        skeptic = self._ask("skeptic", agents.SKEPTIC, f"Pair: {ev.instrument}\nThe case so far:\n{_dump(case)}",
                            agents.SKEPTIC_SCHEMA)
        transcript["skeptic"] = skeptic

        # 4. The risk manager sizes each direction against what's already open.
        risk = self._ask("risk_manager", agents.RISK, self._risk_prompt(ev, account), agents.RISK_SCHEMA)
        risk["long_size"] = _clamp(risk["long_size"], 0.0, 1.0)
        risk["short_size"] = _clamp(risk["short_size"], 0.0, 1.0)
        transcript["risk_manager"] = risk

        # 5. The head of desk decides on the arguments and must answer every serious objection.
        head_prompt = (
            f"Pair: {ev.instrument}\nThe full debate:\n{_dump({**case, 'skeptic': skeptic, 'risk_manager': risk})}\n\n"
            f"Analyst track records (directional calls scored after their time horizon):\n"
            f"{_dump(self.track_records) if self.track_records else 'None scored yet: weigh arguments only.'}"
        )
        head = self._ask("head_of_desk", agents.HEAD, head_prompt, agents.HEAD_SCHEMA, head=True)
        transcript["head_of_desk"] = head

        outcome, proposal = self._enforce(ev, debate_id, skeptic, risk, head)
        transcript["outcome"] = outcome
        return DebateResult(
            debate_id, ev.instrument, proposal, outcome, transcript,
            initial_stances={k: v["stance"] for k, v in views.items()},
            final_stances={k: v["stance"] for k, v in final.items()},
            stance_changes=changes,
        )

    def _risk_prompt(self, ev: Evidence, account: AccountState) -> str:
        mine = set(currencies(ev.instrument))
        open_trades = [
            {
                "instrument": t.instrument,
                "side": "long" if t.units > 0 else "short",
                "risk_at_stop": round(t.risk_amount, 2),
                "shares_a_currency": bool(mine & set(currencies(t.instrument))),
            }
            for t in account.open_trades
        ]
        limits = self.settings.risk
        return (
            f"Proposed pair: {ev.instrument}\n"
            f"Budget equity: {account.nav:.2f} {account.currency}\n"
            f"Normal risk per trade: {limits.risk_per_trade_pct}% of budget; total open risk cap "
            f"{limits.max_total_risk_pct}%; max {limits.max_open_trades} open trades; daily loss limit "
            f"{limits.daily_loss_limit_pct}%.\n"
            f"Open trades:\n{_dump(open_trades) if open_trades else 'None'}\n"
            f"Scheduled events for this pair:\n{_dump(ev.calendar) if ev.calendar else 'Unknown'}\n"
            f"Volatility (ATR14 in pips) by timeframe: "
            f"{_dump({tf: c['summary']['atr14_pips'] for tf, c in ev.charts.items()})}"
        )

    def _enforce(self, ev: Evidence, debate_id: str, skeptic: dict, risk: dict, head: dict):
        """The checkable rules. Returns (outcome, proposal or None)."""
        if head["decision"] != "trade" or head["side"] == "none":
            return "no_trade: head of desk passed", None
        answered = {a["objection_id"] for a in head["objection_answers"] if a["resolved"]}
        open_objections = [
            o["id"] for o in skeptic["objections"] if o["severity"] in ("fatal", "serious") and o["id"] not in answered
        ]
        if open_objections:
            return f"no_trade: unanswered objections {', '.join(open_objections)}", None
        size = risk["long_size"] if head["side"] == "long" else risk["short_size"]
        if size <= 0:
            return f"no_trade: risk manager blocked the {head['side']} side", None
        if head["stop_loss"] is None:
            return "no_trade: no stop loss given", None
        proposal = TradeProposal(
            instrument=ev.instrument,
            side=Side.BUY if head["side"] == "long" else Side.SELL,
            stop_loss=float(head["stop_loss"]),
            take_profit=float(head["take_profit"]) if head["take_profit"] is not None else None,
            rationale=head["reasoning"],
            debate_id=debate_id,
            risk_fraction=size,
        )
        return "trade", proposal
