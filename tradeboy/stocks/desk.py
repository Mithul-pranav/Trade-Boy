"""The stocks desk's loop: while the market is open, look after positions, find events, debate the best."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from ..alerts import Alerts
from ..desk import AGREEMENT_ALARM_MIN_DEBATES, AGREEMENT_ALARM_RATE, CREDITS_RETRY
from ..journal import Journal
from ..llm import AgentError, CreditsExhausted, SpendCapReached
from . import agents
from .debate import StockDebate, StockDebateResult, gather
from .events import EventScanner
from .executor import StockExecutor, hours_for
from .exits import StockTradeManager
from .models import StockEvent
from .team import QuantSeat, RiskQuant, health_check

log = logging.getLogger("tradeboy")


class StockDesk:
    def __init__(self, settings, broker, journal: Journal, llm, *, edgar=None, news=None, earnings=None,
                 dry_run: bool = True, alerts: Alerts | None = None, quant: QuantSeat | None = None):
        self.settings = settings
        self.broker = broker
        self.journal = journal
        self.llm = llm
        self.edgar = edgar
        self.news = news
        self.earnings = earnings
        self.dry_run = dry_run
        self.alerts = alerts or Alerts(journal)
        self.quant = quant
        self.risk_quant = RiskQuant.from_settings(settings)
        self.events = EventScanner(settings, broker, journal, edgar, news)
        self.executor = StockExecutor(settings, broker, journal, earnings, dry_run=dry_run)
        self.positions = StockTradeManager(settings, broker, journal, llm, self.alerts, earnings, self.events, news,
                                           dry_run=dry_run)

    # -- the cycle -----------------------------------------------------------------------------

    def cycle(self, now: datetime) -> None:
        clock = self.broker.clock()
        if not clock.is_open:
            log.info("Market closed; next open %s.", clock.next_open.isoformat(timespec="minutes"))
            return
        self.journal.set_state("last_scan", now.isoformat())
        if self.executor.check_kill_switch(now):
            self.alerts.send(
                "halted",
                f"HALTED. The original {self.settings.starting_capital:g} USD stocks budget is gone, so every "
                "position is closed and nothing new will open. Trading resumes only with your approval "
                "(tradeboy stocks resume --new-budget).", now)
            return
        try:
            self.positions.manage(now, can_spend=lambda: self._budget_ok(now))
        except CreditsExhausted as exc:
            self._credits_out(exc, now)
        except Exception:  # looking after positions must not stop new ones; their stops are at Alpaca
            log.exception("Managing positions failed")
        finally:
            self._journal_usage(now, None)
        self.score_calls(now)
        self._daily_health(now)
        if not self._budget_ok(now):
            return
        left_today = self.settings.claude.max_debates_per_day - self.journal.debates_today(now)
        if left_today <= 0:
            log.info("Daily debate limit reached.")
            return
        try:
            picked = self.pick(now)
        except CreditsExhausted as exc:
            self._credits_out(exc, now)
            picked = []
        except (AgentError, SpendCapReached) as exc:
            log.error("Event triage failed: %s", exc)
            picked = []
        finally:
            self._journal_usage(now, None)
        for event in picked[:left_today]:
            self.debate(event, now)

    def pick(self, now: datetime) -> list[StockEvent]:
        """New events worth a debate. Held stocks go to the exit manager's re-review instead."""
        held = {p.symbol for p in self.broker.positions()}
        cooldown = timedelta(hours=self.settings.claude.debate_cooldown_hours)
        candidates = []
        for event in self.events.scan(now):
            if event.symbol in held:
                self.events.mark_seen(event, now, "held: the exit manager reviews it")
                continue
            last = self.journal.last_debate(event.symbol)
            if last and now - last < cooldown:
                continue
            candidates.append(event)
        limit = self.settings.claude.max_events_per_scan
        if len(candidates) <= limit or candidates[limit - 1].priority > candidates[limit].priority:
            picked = candidates[:limit]
        else:
            answer = self.llm.ask(
                agent="scanner", model=self.settings.claude.scanner_model, system=agents.SCANNER,
                prompt=(f"Time (UTC): {now.isoformat(timespec='minutes')}\nPick at most {limit} of these events "
                        f"for a full debate, or none.\n"
                        + json.dumps([{"key": e.key, "symbol": e.symbol, "kind": e.kind, "summary": e.summary}
                                      for e in candidates], indent=1)),
                schema=agents.SCAN_SCHEMA, effort="low",
            )
            by_key = {e.key: e for e in candidates}
            picked = [by_key[p["key"]] for p in answer["picked"] if p["key"] in by_key][:limit]
        log.info("Events picked: %s", ", ".join(e.summary for e in picked) or "none")
        return picked

    # -- one debate ------------------------------------------------------------------------------

    def debate(self, event: StockEvent, now: datetime) -> StockDebateResult | None:
        if self.journal.is_halted():
            log.warning("Desk is halted; no debate.")
            return None
        if not self._budget_ok(now):
            return None
        log.info("Debate on %s starting (%s).", event.symbol, event.summary)
        related = self.events.related(event.symbol, now)
        try:
            nav = self.executor.equity()
            open_positions = self.executor.open_positions()
            evidence = gather(self.settings, self.broker, event, now, edgar=self.edgar, news=self.news,
                              earnings=self.earnings, other_events=related, risk_quant=self.risk_quant,
                              open_positions=open_positions, nav=nav)
            result = StockDebate(self.settings, self.llm, self._track_records(), self.quant).run(
                evidence, nav, open_positions, now)
        except CreditsExhausted as exc:
            self._credits_out(exc, now)
            self._journal_usage(now, None)
            return None
        except (AgentError, SpendCapReached) as exc:
            log.error("Debate on %s failed: %s", event.symbol, exc)
            self._journal_usage(now, None)
            return None

        execution = self.executor.execute(result.proposal, now, max_share=result.max_share, daily_bars=evidence.daily)
        decision = execution.decision
        p = result.proposal
        side = "long" if p.side.value == "buy" else "short"
        if not decision.approved:
            result.outcome += "; no order: hard limits rejected it: " + "; ".join(decision.reasons)
        elif execution.trade_id:
            result.outcome += f"; opened {execution.filled_qty:g} shares at {execution.fill_price}"
            self.alerts.send(
                f"opened:{execution.trade_id}",
                f"Opened {p.symbol} {side}, {abs(execution.filled_qty):g} shares at {execution.fill_price}, stop "
                f"{p.stop_loss}, target {p.take_profit or 'none'}, risking {decision.risk_amount:.2f} USD. "
                f"Event: {event.summary[:160]}. Exit plan: {p.exit_type}, closed after {p.hold_days} trading days "
                f"at the latest. {p.exit_reason[:250]}", now)
        elif decision.reasons:
            result.outcome += "; no order: " + "; ".join(decision.reasons)
        else:
            result.outcome += f"; dry run, would open {decision.qty:g} shares"
        result.transcript["execution"] = {"approved": decision.approved, "qty": decision.qty,
                                          "risk_amount": decision.risk_amount, "reasons": decision.reasons,
                                          "trade_id": execution.trade_id, "dry_run": self.dry_run}
        result.transcript["outcome"] = result.outcome

        self.journal.record_debate(
            result.debate_id, now, event.symbol, result.outcome,
            agreed_first=len(set(result.initial_stances.values())) == 1,
            agreed_last=len(set(result.final_stances.values())) == 1,
            stance_changes=result.stance_changes, transcript=result.transcript,
        )
        for e in [event, *related]:
            self.events.mark_seen(e, now, result.debate_id)
        self._record_calls(result, evidence.quote.mid, now)
        self._journal_usage(now, result.debate_id)
        self._agreement_alarm(now)
        log.info("Debate %s: %s", result.debate_id, result.outcome)
        return result

    # -- track records (scored in basis points of price) ---------------------------------------

    def _record_calls(self, result: StockDebateResult, mid: float, now: datetime) -> None:
        views = result.transcript["independent_views"]
        calls = [(a, s, result.transcript["rebuttals"][a]["final_confidence"], views[a]["hold_days"])
                 for a, s in result.final_stances.items()]
        quant = result.transcript.get("quant_seat")
        if quant and quant.get("significant"):
            calls.append(("quant", quant["stance"], quant.get("confidence", 55), quant["hold_days"]))
        head = result.transcript["head_of_desk"]
        calls.append(("head_of_desk", head["side"], 100, head["hold_days"]))
        for agent, stance, confidence, days in calls:
            days = min(max(int(days), 1), self.settings.max_hold_days)
            self.journal.record_call(result.debate_id, agent, result.symbol, stance, confidence, mid, now,
                                     now + timedelta(hours=hours_for(now, days)))

    def score_calls(self, now: datetime) -> None:
        for debate_id, agent, symbol, stance, price, due in self.journal.calls_due(now):
            try:
                later = self.broker.price_at(symbol, due)
            except Exception as exc:
                log.error("Scoring %s's call on %s failed: %s", agent, symbol, exc)
                continue
            if later is None:
                continue
            bps = (later / price - 1) * 10_000 * (1 if stance == "long" else -1)
            self.journal.score_call(debate_id, agent, round(bps, 1))

    def _track_records(self) -> dict:
        return {a: {"scored_calls": r["scored_calls"], "hit_rate_pct": r["hit_rate_pct"], "avg_bps": r["avg_pips"]}
                for a, r in self.journal.track_records().items()}

    # -- spending, health and alarms -------------------------------------------------------------

    def _budget_ok(self, now: datetime) -> bool:
        paused = self.journal.get_state("credits_paused_until")
        if paused and now < datetime.fromisoformat(paused):
            log.info("Claude API credits ran out; debates paused until %s.", paused)
            return False
        claude = self.settings.claude
        left = min(claude.monthly_budget_usd - self.journal.month_spend(now),
                   claude.daily_budget_usd - self.journal.day_spend(now))
        if left <= 0:
            monthly = self.journal.month_spend(now) >= claude.monthly_budget_usd
            self.alerts.send(
                "spend_cap",
                f"The {'monthly' if monthly else 'daily'} Claude API spend cap "
                f"({claude.monthly_budget_usd if monthly else claude.daily_budget_usd} USD) is reached. Debates pause "
                "until it resets; open positions keep their stops at Alpaca.", now)
            return False
        self.llm.budget_left_usd = left
        return True

    def _daily_health(self, now: datetime) -> None:
        if self.journal.get_state("health_day") == now.date().isoformat():
            return
        self.journal.set_state("health_day", now.date().isoformat())
        problems = health_check(self.settings, self.broker, self.journal, now)
        self.journal.set_state("health", json.dumps({"time": now.isoformat(), "problems": problems}))
        if problems:
            self.alerts.send("health", "Quant developer health check found problems: " + " ".join(problems)[:900], now)

    def _credits_out(self, exc: Exception, now: datetime) -> None:
        self.journal.set_state("credits_paused_until", (now + CREDITS_RETRY).isoformat())
        self.alerts.send(
            "credits",
            "The Claude API credits have run out (or the API key stopped working), so the agents can't debate. "
            "No new positions will open; open ones keep their stops at Alpaca. Add credits at console.anthropic.com "
            f"and the desk retries every 6 hours. Details: {str(exc)[:200]}", now)

    def _journal_usage(self, now: datetime, debate_id: str | None) -> None:
        usage = self.llm.drain_usage()
        if usage:
            self.journal.record_usage(now, debate_id, usage)

    def _agreement_alarm(self, now: datetime) -> None:
        stats = self.journal.agreement_rate()
        if stats and stats[1] >= AGREEMENT_ALARM_MIN_DEBATES and stats[0] >= AGREEMENT_ALARM_RATE:
            self.alerts.send(
                "agreement",
                f"Agreement alarm: the stock analysts started on the same side in {stats[0]:.0%} of the last "
                f"{stats[1]} debates. They may be echoing each other; the prompts need a review.", now)
            self.journal.set_state("agreement_alarm", f"{stats[0]:.2f} over {stats[1]} debates")
