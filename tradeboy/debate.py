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
from .models import AccountState, Quote, Side, TradeProposal, currencies, pip_size

ANALYSTS = {
    "technical": (agents.TECHNICAL, agents.TECHNICAL_SCHEMA, 0),
    "macro": (agents.MACRO, agents.VIEW_SCHEMA, 1),
    "positioning": (agents.POSITIONING, agents.VIEW_SCHEMA, 1),
}
TIMEFRAMES = {"M15": 200, "H1": 200, "H4": 200, "D": 100}
MIN_SIZE = 0.25  # smallest share of the normal risk the risk manager or head of desk can choose
MIN_TRADE_SIZE = 0.1  # floor after unanswered objections have halved the size


@dataclass
class Evidence:
    """What each analyst gets to look at. Each one gets a different slice, so they bring different facts."""

    instrument: str
    quote: Quote
    charts: dict[str, dict] = field(default_factory=dict)
    position_book: dict | None = None
    calendar: list[dict] = field(default_factory=list)
    news: list[dict] = field(default_factory=list)
    macro: list[dict] = field(default_factory=list)
    # Inputs that should have been there but weren't, so agents treat silence as missing data, not calm.
    data_gaps: list[str] = field(default_factory=list)
    scanner_reason: str = ""


@dataclass
class DebateResult:
    debate_id: str
    instrument: str
    proposal: TradeProposal | None
    outcome: str  # "trade: ..." with how the rules shaped it, or later why the hard limits stopped it
    transcript: dict
    initial_stances: dict[str, str]
    final_stances: dict[str, str]
    stance_changes: int


def _headline(h) -> dict:
    row = {"time": h.time.isoformat(timespec="minutes"), "source": h.source + (f" / {h.publisher}" if h.publisher else ""),
           "headline": h.title, "currencies": list(h.currencies)}
    if h.sentiment:
        row["finbert_tone"] = f"{h.sentiment['label']} {h.sentiment['confidence']:.2f}"
    return row


def gather_evidence(broker, calendar, instrument: str, now: datetime, scanner_reason: str = "", news=None,
                    macro=None) -> Evidence:
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
        if getattr(calendar, "last_error", None):
            evidence.data_gaps.append(calendar.last_error)
    else:
        evidence.data_gaps.append("No economic calendar is configured, so scheduled events are unknown.")
    if news is not None:
        evidence.news = [_headline(h) for h in news.headlines(set(currencies(instrument)), now)]
        evidence.data_gaps.extend(news.gaps(set(currencies(instrument))))
    if macro is not None:
        pair = set(currencies(instrument))
        evidence.macro = macro.snapshot(pair, now)
        evidence.data_gaps.extend(macro.gaps(pair))
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
        gaps = f"\nMissing data (treat as unknown, not as calm):\n{_dump(ev.data_gaps)}\n" if ev.data_gaps else ""
        if name == "macro":
            cal = _dump(ev.calendar) if ev.calendar else "None listed."
            news = _dump(ev.news) if ev.news else "None found."
            macro = (f"\nLatest macro data from FRED (value, date of the reading, change over about a month):\n"
                     f"{_dump(ev.macro)}\n") if ev.macro else ""
            return (head + f"\nScheduled events for these currencies (next 48 hours):\n{cal}\n"
                    f"\nRecent headlines from news feeds (last 48 hours, newest first):\n{news}\n" + macro + gaps)
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
                "news": ev.news,
                "macro": ev.macro,
                "data_gaps": ev.data_gaps,
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
            "data_gaps": ev.data_gaps,
        }
        skeptic = self._ask("skeptic", agents.SKEPTIC, f"Pair: {ev.instrument}\nThe case so far:\n{_dump(case)}",
                            agents.SKEPTIC_SCHEMA)
        transcript["skeptic"] = skeptic

        # 4. The risk manager sizes each direction against what's already open.
        risk = self._ask("risk_manager", agents.RISK, self._risk_prompt(ev, account), agents.RISK_SCHEMA)
        risk["long_size"] = _clamp(risk["long_size"], MIN_SIZE, 1.0)
        risk["short_size"] = _clamp(risk["short_size"], MIN_SIZE, 1.0)
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
        """The checkable rules. Every debate ends in a trade; the rules shape its size and stop.

        Returns (outcome, proposal).
        """
        side = Side.BUY if head["side"] == "long" else Side.SELL
        answered = {a["objection_id"] for a in head["objection_answers"] if a["resolved"]}
        open_objections = [
            o["id"] for o in skeptic["objections"] if o["severity"] in ("fatal", "serious") and o["id"] not in answered
        ]
        allowed = risk["long_size"] if side is Side.BUY else risk["short_size"]
        # Each unanswered fatal or serious objection halves the size.
        size = max(allowed * _clamp(head["size"], MIN_SIZE, 1.0) * 0.5 ** len(open_objections), MIN_TRADE_SIZE)
        stop, stop_note = self._stop(ev, side, head["stop_loss"])
        entry = ev.quote.ask if side is Side.BUY else ev.quote.bid
        target = head["take_profit"]
        if target is not None and (target - entry) * (1 if side is Side.BUY else -1) <= 0:
            target = None
        proposal = TradeProposal(
            instrument=ev.instrument,
            side=side,
            stop_loss=stop,
            take_profit=float(target) if target is not None else None,
            rationale=head["reasoning"],
            debate_id=debate_id,
            risk_fraction=round(size, 3),
        )
        notes = [f"{head['side']} at {size:.0%} size"]
        if open_objections:
            notes.append(f"halved for unanswered objections {', '.join(open_objections)}")
        if stop_note:
            notes.append(stop_note)
        return "trade: " + "; ".join(notes), proposal

    def _stop(self, ev: Evidence, side: Side, stop: float | None) -> tuple[float, str]:
        """The head's stop if it's usable; otherwise one set from volatility. Every order has a stop."""
        entry = ev.quote.ask if side is Side.BUY else ev.quote.bid
        pip = pip_size(ev.instrument)
        sign = -1 if side is Side.BUY else 1
        minimum = (self.settings.risk.min_stop_pips + 1) * pip
        if stop is not None and (stop - entry) * sign >= minimum:
            return float(stop), ""
        atr = ev.charts.get("H1", {}).get("summary", {}).get("atr14_pips")
        distance = max(1.5 * atr * pip if atr else 0.0, minimum)
        if stop is not None and (stop - entry) * sign > 0:
            note = f"stop widened to the {self.settings.risk.min_stop_pips:g}-pip minimum"
            distance = minimum
        else:
            note = "stop set from volatility (1.5x H1 ATR) because the head's stop was unusable"
        return round(entry + sign * distance, 5 if pip < 0.01 else 3), note
